# Gravação dual-stream: 24x7 em baixa resolução + alta resolução nas detecções

> **Especificação de implementação.** Este documento é o plano completo a ser executado
> numa sessão futura. Não é documentação de usuário: descreve mudanças ainda **não**
> implementadas.
>
> **Base:** todas as referências de arquivo e número de linha são contra a tag
> `v0.18.0-beta3` (commit `344efb6`), conferidas contra o código real dessa tag.
>
> **Como usar:** implementar na ordem de fases de §12 (P0 → P6). P0–P2 são estritamente
> sequenciais; P3/P4 podem ir em paralelo depois da P2; P5 depende da P4. Cada fase tem um
> checkpoint verificável. Ler a lista de riscos em §12 antes de começar qualquer fase — em
> especial os itens 1, 2 e 3, que são modos de falha silenciosos.

## Context

Base: Frigate NVR, tag `v0.18.0-beta3`. Objetivo: reproduzir o comportamento do Blue Iris —
gravar **continuamente 24x7 em baixa resolução** e manter **alta resolução apenas nos
trechos com detecções**.

**Metade disso já funciona hoje.** Com `record.continuous.days: 0`, `record.motion.days: 0`
e `alerts`/`detections` com `retain.days > 0`, o `RecordingMaintainer` já descarta todo
segmento do stream `record` que não sobrepõe um `ReviewSegment`
(`frigate/record/maintainer.py:395-482`).

O que falta é o **segundo stream contínuo de baixa resolução**, hoje impossível porque:

- `CameraRoleEnum` (`frigate/config/camera/ffmpeg.py:99`) só tem `audio`/`record`/`detect`,
  e `CameraFfmpegConfig.validate_roles` proíbe repetir um role.
- `Recordings` (`frigate/models.py:69`) não tem dimensão de stream: `path` é único e o
  layout em disco é `RECORD_DIR/%Y-%m-%d/%H/{camera}/%M.%S.mp4`.
- Todo o caminho de leitura (`vod_ts`, `/{camera}/recordings`, `unavailable`, export)
  filtra só por `camera` — dois streams se intercalariam e quebrariam a playlist HLS.

Resultado esperado: o usuário declara um segundo input de baixa resolução com um novo role;
ele grava 24x7 com retenção própria; o player ganha um seletor de stream; e instalações
single-stream existentes continuam idênticas, sem mudança de config.

## Decisões já tomadas pelo usuário

1. **Seletor manual de stream agora.** O fallback automático por segmento (HD onde existe,
   LD no resto) fica para uma fase posterior, mas o modelo de dados e a API não podem
   impedi-lo.
2. **Config = novo role + novo bloco de retenção**, não um sistema genérico de N streams.
3. Escopo: backend completo, API de reprodução, frontend (player/timeline) e export.

---

## 0. Nomenclatura e módulo base

Role **`record_secondary`**, bloco `record.secondary`, tokens `"primary"` / `"secondary"`.

Rationale contra o nome alternativo `record_continuous`: `RecordConfig.continuous` já existe
como *chave de retenção* do stream principal, e o bloco novo terá a sua própria chave
`continuous:`. Ter `record.continuous.days` + role `record_continuous` +
`record.continuous_stream.continuous.days` é genuinamente confuso em docs e mensagens de
erro. "Secondary" também continua honesto se alguém configurar o stream secundário com
`continuous.days: 0` e retenção só por evento (setup invertido, legítimo). **Os rótulos da
UI continuam dizendo "Contínuo / Baixa resolução"** — o nome técnico não vaza para o usuário
final.

Novo módulo `frigate/record/types.py` (espelha `frigate/review/types.py`), **sem importar
`frigate.config`** para que `models.py`, `storage.py` e a API possam importá-lo sem ciclo:

```python
"""Types shared by the recording pipeline."""

from enum import Enum


class RecordStreamEnum(str, Enum):
    primary = "primary"
    secondary = "secondary"


# role name (str) -> stream. Kept as plain strings to avoid importing config.
ROLE_TO_STREAM: dict[str, RecordStreamEnum] = {
    "record": RecordStreamEnum.primary,
    "record_secondary": RecordStreamEnum.secondary,
}


def cache_segment_prefix(camera: str, stream: RecordStreamEnum) -> str:
    """Cache filename prefix for a camera/stream (primary keeps the legacy name)."""
    return camera if stream == RecordStreamEnum.primary else f"{camera}#{stream.value}"


def parse_cache_filename(basename: str) -> tuple[str, RecordStreamEnum] | None:
    """Parse a cache basename (no extension) into (camera, stream), or None."""
    try:
        left, _date = basename.rsplit("@", maxsplit=1)
    except ValueError:
        return None

    if "#" in left:
        camera, raw = left.split("#", 1)
        try:
            return camera, RecordStreamEnum(raw)
        except ValueError:
            return None

    return left, RecordStreamEnum.primary
```

O parser deliberadamente **não** faz o `strptime` da data: os dois call sites em
`move_files` já fazem isso e duplicar a lógica de timezone seria pior.

---

## 1. Configuração

### 1.1 `frigate/config/camera/ffmpeg.py`

```python
class CameraRoleEnum(str, Enum):
    audio = "audio"
    record = "record"
    record_secondary = "record_secondary"
    detect = "detect"
```

Novo campo em `FfmpegOutputArgsConfig` (mesmo default do `record`, para menor surpresa;
quem quiser `-an` na baixa resolução usa `preset-record-generic`):

```python
record_secondary: str | list[str] = Field(
    default=RECORD_FFMPEG_OUTPUT_ARGS_DEFAULT,
    title="Secondary record output arguments",
    description="Default output arguments for the secondary (continuous) record role stream.",
)
```

Em `CameraFfmpegConfig.validate_roles`, as regras existentes ("cada role no máximo uma vez",
"detect obrigatório") já funcionam com o membro novo do enum sem alteração. Adicionar uma:

```python
if "record_secondary" in roles and "record" not in roles:
    raise ValueError(
        "The record_secondary role requires the record role to also be assigned."
    )
```

Isso mantém `record` como o primary canônico e evita um config "só secundário" que
confundiria todos os defaults a jusante.

### 1.2 `frigate/config/camera/record.py`

```python
class RecordSecondaryConfig(FrigateBaseModel):
    enabled: bool = Field(default=False, title="Enable secondary recording", ...)
    continuous: RecordRetainConfig = Field(default_factory=RecordRetainConfig, ...)
    motion: RecordRetainConfig = Field(default_factory=RecordRetainConfig, ...)
    enabled_in_config: bool | None = Field(default=None, ...)
```

Deliberadamente **fora** do bloco: `detections`/`alerts`/`export`/`preview`/`expire_interval`.
Pre/post capture de review, export e geração de preview continuam no nível da câmera, e o
stream secundário reusa `RecordConfig.get_review_pre_capture/post_capture`.

Em `RecordConfig`, adicionar `secondary: RecordSecondaryConfig` e os três helpers que
passam a ser **o único lugar** onde o resto do código pergunta "qual retenção vale para o
stream X":

```python
def get_retention(
    self, stream: RecordStreamEnum
) -> tuple[RecordRetainConfig, RecordRetainConfig]:
    """Return (continuous, motion) retention for the given stream."""
    if stream == RecordStreamEnum.secondary:
        return self.secondary.continuous, self.secondary.motion
    return self.continuous, self.motion

def stream_enabled(self, stream: RecordStreamEnum) -> bool:
    if stream == RecordStreamEnum.secondary:
        return self.enabled and self.secondary.enabled
    return self.enabled

def enabled_streams(self) -> list[RecordStreamEnum]:
    return [s for s in RecordStreamEnum if self.stream_enabled(s)]

def timeline_stream(self) -> RecordStreamEnum:
    """The stream with the broadest continuous coverage; drives timeline visuals."""
    if self.secondary.enabled and self.secondary.continuous.days >= self.continuous.days:
        return RecordStreamEnum.secondary
    return RecordStreamEnum.primary
```

`secondary.enabled` fica sob o `record.enabled` mestre: desligar gravação em runtime tem
que matar os dois streams.

### 1.3 `frigate/config/camera/camera.py:206` — guarda no auto-role de input único

Hoje `__init__` sobrescreve incondicionalmente `roles = ["record", "detect"]` quando há um
input só. Se um usuário com um input habilitar `record.secondary`, a lista dele é destruída
e o recurso silenciosamente não faz nada.

```python
if len(config["ffmpeg"]["inputs"]) == 1:
    roles = config["ffmpeg"]["inputs"][0].get("roles", [])
    if "record_secondary" in roles:
        raise ValueError(
            "record_secondary requires a second input; a camera with a single "
            "input cannot record two streams."
        )
    ...  # sobrescrita existente
```

**A guarda precisa estar dentro do `__init__`**, não em `verify_config_roles`: a
sobrescrita acontece antes da validação, então quando `verify_config_roles` roda a
evidência já foi apagada.

### 1.4 `frigate/config/config.py`

`verify_config_roles()` (linha 247):

```python
if camera_config.record.secondary.enabled and "record_secondary" not in assigned_roles:
    raise ValueError(
        f"Camera {camera_config.name} has record.secondary enabled, but "
        "record_secondary is not assigned to an input."
    )
if "record_secondary" in assigned_roles and not camera_config.record.secondary.enabled:
    logger.warning(
        "Camera %s has the record_secondary role assigned but record.secondary.enabled "
        "is false, ignoring the input",
        camera_config.name,
    )
```

`verify_recording_segments_setup_with_reasonable_time()` (linha 278): extrair o corpo para
`_verify_segment_args(camera_name, record_args, label)` e chamar para `output_args.record`
e, quando o role estiver presente, para `output_args.record_secondary`. **Sem isso, um
`-segment_time 3600` custom no secundário passa direto pela guarda de 60s** e estoura
`MAX_SEGMENTS_IN_CACHE` e o `/tmp/cache`.

Linha 866 (`camera_config.record.enabled_in_config = camera_config.record.enabled`) —
adicionar o espelho:
```python
camera_config.record.secondary.enabled_in_config = camera_config.record.secondary.enabled
```

### 1.5 `frigate/config/camera/updater.py:130`

```python
elif update_type == CameraConfigUpdateEnum.record:
    old_enabled = config.record.enabled_in_config
    old_secondary = config.record.secondary.enabled_in_config
    config.record = updated_config
    if (
        old_enabled != updated_config.enabled_in_config
        or old_secondary != updated_config.secondary.enabled_in_config
    ):
        config.recreate_ffmpeg_cmds()
```

E em `CameraWatchdog` (`frigate/video/ffmpeg.py`, ~linhas 160/310) rastrear
`self.was_secondary_enabled_in_config` junto de `was_record_enabled_in_config` e reiniciar
quando qualquer um mudar.

### 1.6 YAML alvo (documentar como o setup padrão)

```yaml
cameras:
  front_door:
    ffmpeg:
      output_args:
        record: preset-record-generic-audio-aac
        record_secondary: preset-record-generic      # opcional; -an na baixa resolução
      inputs:
        - path: rtsp://cam/main
          roles: [record]
        - path: rtsp://cam/sub
          roles: [detect, record_secondary]          # uma conexão para o substream
    record:
      enabled: true
      # alta resolução: só em torno de review items
      continuous: { days: 0 }
      motion:     { days: 0 }
      alerts:     { retain: { days: 30, mode: motion } }
      detections: { retain: { days: 14, mode: motion } }
      # baixa resolução: 24x7
      secondary:
        enabled: true
        continuous: { days: 60 }
        motion:     { days: 60 }
```

`roles: [detect, record_secondary]` no mesmo input é o layout a documentar como padrão
porque usa **uma** conexão RTSP em vez de duas. Já é comprovadamente seguro: o config de
input único mais comum hoje é `roles: [record, detect]`, que percorre exatamente o mesmo
caminho — hwaccel de decode é adicionado (`camera.py:337`), a saída `pipe:` rawvideo consome
os frames decodificados, e o muxer de segmento usa `-c copy` sobre o bitstream original.

---

## 2. Geração do comando ffmpeg

### 2.1 Esquema de nome no cache

| stream | arquivo em `CACHE_DIR` |
|---|---|
| primary (**inalterado**) | `{camera}@{CACHE_SEGMENT_FORMAT}.mp4` |
| secondary | `{camera}#secondary@{CACHE_SEGMENT_FORMAT}.mp4` |

Por que `#`:
- `REGEX_CAMERA_NAME = ^[a-zA-Z0-9_-]+$` (`const.py:104`) garante que `#` nunca aparece num
  nome de câmera, então `left.split("#", 1)` depois do `rsplit("@", 1)` existente é
  inequívoco.
- `#` não tem significado para `-strftime 1` nem para o muxer de segmento, e o alvo de saída
  é um único elemento de argv (sem shell).
- **Retrocompatibilidade no upgrade é grátis**: arquivos já em `/tmp/cache` como
  `{camera}@{ts}.mp4` são parseados como `primary` sem caso especial — e é exatamente o que
  eles são.
- Manter o nome do primary intacto também significa que a checagem de arquivo em uso via
  psutil (`maintainer.py:158-168`, que compara `nt.path.split("/")[-1]`) e o skip do
  prefixo `preview_` não mudam.

### 2.2 `camera.py::_get_ffmpeg_cmd`

Inserir o branch espelhando o de record, **antes** dele, para que a ordem de saída fique
secondary → primary → detect (`pipe:` por último):

```python
if (
    "record_secondary" in ffmpeg_input.roles
    and self.record.enabled
    and self.record.secondary.enabled
):
    secondary_args = get_ffmpeg_arg_list(
        parse_preset_output_record(
            self.ffmpeg.output_args.record_secondary,
            self.ffmpeg.apple_compatibility,
        )
        or self.ffmpeg.output_args.record_secondary
    )
    target = os.path.join(
        CACHE_DIR,
        f"{cache_segment_prefix(self.name, RecordStreamEnum.secondary)}"
        f"@{CACHE_SEGMENT_FORMAT}.mp4",
    )
    ffmpeg_output_args = secondary_args + [target] + ffmpeg_output_args
```

O branch existente muda só para usar `cache_segment_prefix(self.name,
RecordStreamEnum.primary)` — que retorna `self.name` (no-op), mas centraliza a convenção.

`_build_ffmpeg_cmds` (linha 257) não muda: quando `record_secondary` está num input próprio
gera uma segunda entrada em `_ffmpeg_cmds` com `roles: [record_secondary]`; quando divide o
substream com `detect`, gera uma entrada com os dois roles. `start_all_ffmpeg`
(`ffmpeg.py:554`) já spawna tudo que não é o cmd de detect, e o nome do logpipe
`ffmpeg.{camera}.{'_'.join(sorted(roles))}` vira `ffmpeg.front_door.record_secondary` —
legível, sem mudança.

### 2.3 `frigate/util/builtin.py:140`

```python
def get_record_segment_time(
    config: "CameraConfig",
    stream: RecordStreamEnum = RecordStreamEnum.primary,
) -> int:
    args = (
        config.ffmpeg.output_args.record_secondary
        if stream == RecordStreamEnum.secondary
        else config.ffmpeg.output_args.record
    )
    ...
```

O default mantém o call site existente (`ffmpeg.py:171`) compilando; esse call site então
vira um dict por stream (§3.2).

---

## 3. IPC e watchdog por stream

### 3.1 `frigate/comms/recordings_updater.py` — mudança de aridade

Todos os payloads passam de `(camera, timestamp, cache_path)` para
`(camera, stream, timestamp, cache_path)`, com `stream` como **string** (`.value`) para
manter o formato de fio trivial.

Adicionar um helper tipado para que ninguém monte tupla à mão:

```python
class RecordingsDataPublisher(Publisher[Any]):
    def publish_segment(
        self,
        camera: str,
        stream: str,
        timestamp: float | None,
        cache_path: str | None,
        sub_topic: str,
    ) -> None:
        super().publish((camera, stream, timestamp, cache_path), sub_topic)
```

**Os dois consumidores têm que mudar no mesmo commit** (verificado — são exatamente dois):
- `frigate/video/ffmpeg.py:162` / desempacotamento em ~338
- `frigate/embeddings/maintainer.py:153` (tópico `saved`)

Se um for esquecido, o unpack levanta `ValueError` dentro do loop `while True` de drenagem
do `CameraWatchdog` e **a thread do watchdog morre** — o monitoramento de saúde de gravação
para silenciosamente para *todas* as câmeras, sem log apontando a causa. Mitigação: renomear
o método de publish (`publish` → `publish_segment`) para que qualquer caller desatualizado
falhe alto com `AttributeError` em vez de silenciosamente com shape errado.

### 3.2 `CameraWatchdog` — saúde por stream

Trocar os três escalares (`ffmpeg.py:163-165`) por dicts indexados por valor de stream:

```python
self.latest_valid_segment_time: dict[str, float] = defaultdict(float)
self.latest_invalid_segment_time: dict[str, float] = defaultdict(float)
self.latest_cache_segment_time: dict[str, float] = defaultdict(float)
self.record_stale_threshold: dict[str, int] = {
    s.value: max(120, 2 * get_record_segment_time(self.config, s) + 30)
    for s in RecordStreamEnum
}
```

O loop de drenagem (linhas 329-357) desempacota `camera, stream, segment_time, _` e escreve
em `dict[stream]`. Os três pontos de reset (linhas 280-282, 294-296, 318-320) viram
`.clear()`.

O bloco de monitoramento (linhas 401-490) troca a guarda
`if self.config.record.enabled and "record" in p["roles"]` por resolução de stream por
processo:

```python
proc_streams = [ROLE_TO_STREAM[r] for r in p["roles"] if r in ROLE_TO_STREAM]
for stream in proc_streams:
    if not self.config.record.stream_enabled(stream):
        continue
    # ... matemática de staleness usando self.latest_*[stream.value]
    #     e self.record_stale_threshold[stream.value]
    if cache_stale or valid_stale or invalid_stale:
        # reinicia o processo; um restart por processo, não por stream
        break
```

`record_enable_time` (grace period de 90s) pode continuar sendo um valor único por câmera —
os dois streams sobem juntos em todo caminho de restart.

`_send_record_status` vira `_send_record_status(stream, status, now)` e publica em
`{camera}/status/record` para primary e `{camera}/status/record_secondary` para secondary.
O caminho de restart em 469-472/482-485 já publica `f"{camera}/status/{role.value}"` por
role, então o role novo produz o tópico novo automaticamente; o fall-through de
`dispatcher.py:393` encaminha tópicos desconhecidos para MQTT/websocket, então nada quebra —
mas precisa de entrada em `docs/docs/integrations/mqtt.md`.

**Ganho principal:** hoje um primary saudável mantém `latest_valid_segment_time` fresco, de
modo que um secondary morto nunca seria detectado. Os dicts por stream corrigem isso.

⚠️ Ver risco #2 em §12: no layout recomendado, `record_secondary` divide o input com
`detect`, e esse processo é gerenciado por `start_ffmpeg_detect()` — ele **não** entra em
`ffmpeg_other_processes` e portanto escapa desse monitor. Tratar explicitamente na P2:
`start_ffmpeg_detect` precisa registrar o processo de detect na lista monitorada quando ele
carregar um role de record, ou o monitor precisa considerá-lo separadamente.

---

## 4. `RecordingMaintainer`

### 4.1 Parsing e agrupamento

`move_files()` (linha 106): os dois blocos de parse (119-126 e 178-185) chamam
`parse_cache_filename`. `newest_cache_segments` e `grouped_recordings` passam a ser
indexados por `tuple[str, RecordStreamEnum]`.

O bloco "publish None for cameras with no cache files" (149-155) tem que virar por stream
habilitado, senão o watchdog nunca descobre que o secundário sumiu:

```python
for camera_name, cam in self.config.cameras.items():
    for stream in cam.record.enabled_streams():
        if (camera_name, stream) not in newest_cache_segments:
            self.recordings_publisher.publish_segment(
                camera_name, stream.value, None, None,
                RecordingsDataTypeEnum.latest.value,
            )
```

### 4.2 Decisão de chaveamento: `object_recordings_info` continua por **câmera**

Motion boxes, tracked objects, regions e dBFS vêm todos do stream de detecção e descrevem
uma *janela de tempo*, não um arquivo. Segmentos dos dois streams cobrindo o mesmo wall
clock recebem estatísticas idênticas, e `segment_stats(camera, start, end)` não precisa de
parâmetro de stream. Chavear por `(camera, stream)` dobraria memória e exigiria que o
subscriber de detecção fizesse fan-out de uma cópia por stream, sem benefício.

**Mas** três bookkeepings hoje chaveados por câmera têm que ir para `(camera, stream)`:

- **Trim de cache (linhas 199-249).** `MAX_SEGMENTS_IN_CACHE = 6` tem que valer *por
  stream*. Agrupado só por câmera, os dois streams brigam por seis slots e o maintainer
  começa a descartar segmentos válidos em operação normal. As mensagens de log devem incluir
  o stream.
- **O publish `saved` (293-304)** — carrega o stream.
- **`_expire_stale_recordings_info` (316)** — o teste `if camera in grouped_recordings` tem
  que ser contra o *conjunto de câmeras* derivado das chaves compostas:
  `cameras_with_segments = {c for c, _ in grouped_recordings}`.

### 4.3 🔴 Os loops de `pop` (linhas 253-267) — correção obrigatória

```python
for camera, recordings in grouped_recordings.items():
    while (
        len(self.object_recordings_info[camera]) > 0
        and self.object_recordings_info[camera][0][0]
        < recordings[0]["start_time"].timestamp()
    ):
        self.object_recordings_info[camera].pop(0)
```

Com dois grupos por câmera, o grupo que iterar primeiro corta
`object_recordings_info[camera]` até *o seu próprio* segmento mais antigo. Se o secundário
estiver alguns segmentos atrás (`-segment_time` diferente, jitter de rede, um restart), a
passada do primary descarta info de frame que o secundário ainda precisa. Aí `segment_stats`
retorna `motion_count=0, active_object_count=0`, e ou `should_discard_segment(motion)`
descarta um segmento que tinha movimento, ou — pior — o segmento é gravado com `motion=0` e
o `expire_existing_camera_recordings` o apaga depois sob `mode == motion`. **Perda de dados
silenciosa e não determinística.**

Correção: içar o trim para fora do loop por grupo e calcular o piso por câmera:

```python
oldest_by_camera: dict[str, float] = {}
for (camera, _stream), recs in grouped_recordings.items():
    ts = recs[0]["start_time"].timestamp()
    oldest_by_camera[camera] = min(oldest_by_camera.get(camera, ts), ts)

for camera, floor_ts in oldest_by_camera.items():
    for info in (self.object_recordings_info[camera], self.audio_recordings_info[camera]):
        while info and info[0][0] < floor_ts:
            info.pop(0)
```

Do mesmo modo, a query de `ReviewSegment` (271-287) deve ser emitida **uma vez por câmera**
usando esse mesmo piso e compartilhada pelos dois grupos — senão são dois round-trips
idênticos ao banco por câmera a cada tick de 5s.

### 4.4 `validate_and_move_segment`

Assinatura: `(self, camera, stream, reviews, recording)`.

- Checagem de habilitado (linha 342): `if camera not in self.config.cameras or not
  self.config.cameras[camera].record.stream_enabled(stream): drop`.
- Publishes carregam `stream.value`.
- Seleção de retenção (391-399):
  ```python
  continuous_cfg, motion_cfg = record_config.get_retention(stream)
  if continuous_cfg.days > 0:
      highest = "continuous"
  elif motion_cfg.days > 0:
      highest = "motion"
  ```
- **O fallthrough de sobreposição com review (436-482) vale para os dois streams, sem
  alteração.** É deliberado: um secundário com `continuous.days: 0` ainda guarda segmentos
  de evento, e — mais importante para o caso de uso principal — o *primary* continua se
  comportando exatamente como hoje. Pre/post capture e retain modes de alerts/detections
  ficam no nível da câmera, compartilhados.
- `move_segment(camera, stream, ...)`.

### 4.5 Layout em disco: subdiretório

| stream | caminho |
|---|---|
| primary (**inalterado**) | `{RECORD_DIR}/%Y-%m-%d/%H/{camera}/%M.%S.mp4` |
| secondary | `{RECORD_DIR}/%Y-%m-%d/%H/{camera}/secondary/%M.%S.mp4` |

Justificativa vs. sufixo no nome (`%M.%S-secondary.mp4`):
- **Unicidade.** `Recordings.path` é `unique`; os dois esquemas satisfazem, mas o subdir
  torna estruturalmente impossível uma colisão com um terceiro stream futuro.
- **Operação.** Dá pra `du -sh` a árvore de baixa resolução, ou bind-mount / rsync dela para
  outro disco, sem globbing. Como o ponto do recurso é exatamente "armazenamento 24x7 barato
  vs. armazenamento de evento caro", isso importa.
- **Nada faz parse de nome de arquivo de gravação.** Tudo lê caminhos absolutos de
  `Recordings.path`. Os três lugares que tocam a estrutura foram verificados:
  - `remove_empty_directories(Path(RECORD_DIR), maybe_empty_dirs)` (`util/media.py:67`)
    sobe por `path.parent` até `root`, então o nível extra é só mais uma iteração.
    `cleanup.py` adiciona `recording_path.parent`, que agora é `.../{camera}/secondary` —
    correto.
  - `sync_recordings` (`util/media.py:196-209`) usa `os.walk(RECORD_DIR)` e compara conjuntos
    de caminhos completos contra os do DB. Pega o subdir automaticamente. A comparação de
    string `root > hour_check` do branch `limited` continua válida:
    `.../2026-08-10/13/cam/secondary` ordena depois de `.../2026-08-10/13`.
  - `camera_cleanup.cleanup_camera_files` — ver §6.3; já quebrado hoje, indiferente.
- Contagem de entradas por diretório não piora (o subdir mantém ~360 arquivos/hora/câmera por
  stream em vez de 720 num diretório só).

---

## 5. Schema e migration 036

### 5.1 `frigate/models.py`

```python
class Recordings(Model):
    ...
    stream = CharField(default="primary", max_length=20, index=False)

    class Meta:
        indexes = ((("camera", "stream", "start_time"), False),)
```

**Não mexer no `id`.** `f"{start_time.timestamp()}-{rand6}"` já usa ~24 caracteres contra
`max_length=30`; anexar um token de stream estoura. O sufixo aleatório de 6 chars já torna
colisão entre streams desprezível.

### 5.2 `migrations/036_add_recording_stream.py`

```python
def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE "recordings" ADD COLUMN "stream" VARCHAR(20) '
        "NOT NULL DEFAULT 'primary'"
    )
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_camera_stream_start_time" '
        'ON "recordings" ("camera", "stream", "start_time")'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX IF EXISTS "recordings_camera_stream_start_time"')
    migrator.sql('ALTER TABLE "recordings" DROP COLUMN "stream"')
```

**Backfill: nenhum necessário.** `ADD COLUMN ... NOT NULL DEFAULT 'primary'` no SQLite com
default constante é uma mudança de metadados O(1) — não reescreve a tabela nem com dezenas de
milhões de linhas, e as linhas existentes passam a ler o default. Toda gravação
pré-upgrade é, por definição, o stream primário. Isso é estritamente melhor que uma coluna
`NULL`, que forçaria todo predicado de primary a lidar com `IS NULL`.

**Índice: justificado.** Hoje o único índice é em `camera` sozinho, então o padrão de acesso
dominante (camera + faixa de tempo, ~15 call sites) faz index scan em camera seguido de
filtro sobre *todo* o histórico daquela câmera. O número de linhas praticamente dobra com
este recurso, o que dobra esse scan. `(camera, stream, start_time)` transforma cada uma
dessas em um range seek direto. O custo único do `CREATE INDEX` num DB grande é real
(dezenas de segundos em tabela de milhões de linhas) mas é pago uma vez no upgrade —
adicionar log antes/depois na migration. Ele também domina estritamente o índice existente
de `camera` para essas queries; considerar remover o índice standalone numa migration
posterior, depois de confirmar que nenhuma query depende dele sozinho.

---

## 6. Os ~26 sites que consultam `Recordings`

### 6.1 Helper compartilhado

Novo `frigate/record/queries.py` (importa só `peewee`, `frigate.models`,
`frigate.record.types` — sem ciclos):

```python
def overlaps(start_ts: float, end_ts: float):
    """Segments overlapping [start_ts, end_ts]."""
    return (Recordings.end_time >= start_ts) & (Recordings.start_time <= end_ts)


def for_stream(stream: RecordStreamEnum | None):
    """None == all streams."""
    return None if stream is None else (Recordings.stream == stream.value)


def camera_range(camera: str, start_ts: float, end_ts: float,
                 stream: RecordStreamEnum | None):
    clauses = [Recordings.camera == camera, overlaps(start_ts, end_ts)]
    s = for_stream(stream)
    if s is not None:
        clauses.append(s)
    return reduce(operator.and_, clauses)
```

Dois pontos sobre `overlaps`:

1. Substitui o predicado de 3 cláusulas `between | between | (contains)` duplicado
   literalmente em `media.py:481-485`, `media.py:577-581`, `record.py:406-410`,
   `export.py:162-167`, `export.py:231-236`, `record/export.py:700-706`, `review.py:551-557`,
   `audio.py:42-46`, `debug_replay.py:104-108`, `motion_search.py:471-484`. É **logicamente
   equivalente** (qualquer sobreposição tem seu início na faixa, seu fim na faixa, ou
   contém a faixa) e é uma conjunção única, então o SQLite consegue usar o índice composto
   novo em vez de decompor o OR. `api/record.py:246-247` já usa essa forma simplificada
   hoje, o que confirma a equivalência na prática.
2. **Não dar default para `stream` em `camera_range`.** Obrigar cada call site a declarar
   intenção é a melhor defesa contra a classe de corrupção silenciosa abaixo.

### 6.2 Tabela de classificação

| # | Site | Classe | Risco se não filtrar |
|---|---|---|---|
| 1 | `api/media.py:312,331` `get_snapshot_from_recording` | **selecionado** (`?stream=`, default primary; fallback ao outro stream se não houver linha) | Baixo — thumbnail na resolução errada |
| 2 | `api/media.py:392` `submit_recording_snapshot_to_plus` | **primary hard-coded** | 🔴 **ALTO** — frame de baixa resolução enviado ao Frigate+ envenena o dataset de treino. Sem sintoma visível. |
| 3 | `api/media.py:475` `recording_clip` (clip.mp4) | **selecionado** | 🔴 **ALTO** — a lista de concat recebe dois segmentos sobrepostos por janela; a saída toca cada momento duas vezes, alternando resolução |
| 4 | `api/media.py:570` `vod_ts` | **selecionado, obrigatório** | 🔴 **MÁXIMO** — clips duplicados no mapping do nginx-vod; `durations` e `segment_duration` errados; timeline HLS dobra |
| 5 | `api/record.py:80` uso de storage | **todos** | nenhum (agregado correto) |
| 6 | `api/record.py:103` `all_recordings_summary` (dias) | **todos** | nenhum (união booleana) |
| 7 | `api/record.py:127,153` resumo horário | **todos, merge por `max`** (ver abaixo) | 🟠 MÉDIO — `SUM(duration)` dobra e as barras alegam 7200s numa hora de 3600s |
| 8 | `api/record.py:234` `/{camera}/recordings` | **selecionado, obrigatório** | 🔴 **MÁXIMO** — alimenta `DynamicVideoController` / `calculateSeekPosition`; linhas duplicadas deslocam todo offset calculado, e clicar na timeline cai no momento errado |
| 9 | `api/record.py:292` `/recordings/unavailable` | **`?stream=` opcional; default união de todos** (preserva o comportamento atual) | Baixo; mas ver §10.3 — este endpoint passa a alimentar o indicador de HD |
| 10 | `api/record.py:432` delete por faixa | **todos**, `?stream=` opcional | nenhum (op admin; apagar os dois é o default sensato) |
| 11 | `api/review.py:549` delete review + recordings | **todos** (inalterado) | nenhum — apagar os dois é o correto |
| 12 | `api/review.py:613` `/review/activity/motion` | **stream único: `record.timeline_stream()`** | 🟠 MÉDIO — ver correção abaixo |
| 13 | `api/export.py:160` `_validate_export_source` | **selecionado** (tem que casar com o que o export puxa) | 🟠 MÉDIO — validação passa para um stream sem dados |
| 14 | `api/export.py:228` validação de batch export | **selecionado** | 🟠 MÉDIO — idem |
| 15 | `record/cleanup.py:110` `expire_existing_camera_recordings` | **loop por stream** | 🔴 **ALTO** — ver §7; janela de retenção errada aplicada a metade das linhas |
| 16 | `record/cleanup.py:283/295` varredura de câmera removida | **todos**, `expire_days` = max sobre os dois configs | 🟠 MÉDIO — linhas órfãs nunca expiram |
| 17 | `record/export.py:686` `get_record_export_command` | **selecionado** (query paginada + URL do VOD) | 🔴 ALTO — os chunks de paginação intercalam streams, produzindo entradas de playlist `-f concat` sobrepostas |
| 18 | `storage.py:45,56` bandwidth | **por stream, depois somado** | 🔴 **ALTO** — ver §7.3; threshold de espaço livre errado → deleção prematura ou disco cheio |
| 19 | `storage.py:90` uso | **todos** | nenhum |
| 20 | `storage.py:129,203` `reduce_storage_consumption` | **ordenado: primary primeiro** (§7.3) | 🟠 MÉDIO — apaga histórico 24x7 de baixa res para salvar um clipe de evento |
| 21 | `util/media.py:117,174,215` `sync_recordings` | **todos** (baseado em path, agnóstico a stream) | nenhum; verificar que o subdir novo é percorrido |
| 22 | `util/audio.py:36` extração de áudio | **primary** | 🔴 **ALTO** — concat sobreposto produz áudio duplicado/gaguejante; a transcrição sai lixo sem erro |
| 23 | `util/classification.py:553` extração de frame | **primary** | 🟠 MÉDIO — classifica um crop de baixa resolução |
| 24 | `util/camera_cleanup.py:52` | **todos** (inalterado) | nenhum |
| 25 | `jobs/motion_search.py:470` | **selecionado, default primary** | 🟠 MÉDIO — cada timestamp decodificado duas vezes; 2× runtime e hits duplicados |
| 26 | `jobs/debug_replay.py:102` `query_recordings` | **primary** | 🟠 MÉDIO — replaya frames de baixa res num pipeline dimensionado para o main |

**Correção à análise da linha 12** (`/review/activity/motion`, `api/review.py:613`):
verificado no código — o resample usa `.max()`, **não** `.sum()`
(`df["motion"].resample(f"{scale}s").max()`). Como os dois streams derivam suas estatísticas
do mesmo `object_recordings_info[camera]`, valores duplicados **não** dobram a intensidade
do movimento. O problema real é mais brando: as fronteiras de segmento dos dois streams
diferem, então `segment_stats` cobre janelas ligeiramente diferentes e o `.max()` pega o
maior dos dois — o sombreamento fica levemente enviesado para cima, e o
`df["camera"].resample(...).agg(lambda x: ",".join(set(x)))` processa o dobro de linhas.
Severidade **MÉDIA**, não alta. Ainda assim filtrar por `record.timeline_stream()`: para o
config alvo (primary continuous=0, secondary continuous=60) isso retorna `secondary` —
exatamente certo, a timeline de movimento segue o stream 24x7.

**Regra de merge do resumo horário (linha 7):** `motion`, `objects` e `duration` são
grandezas por *janela de tempo*, não por *arquivo* — os `segment_stats` dos dois streams
para a mesma janela vêm do mesmo `object_recordings_info[camera]` e são essencialmente
idênticos. Então agrupar por `(hora, stream)` e fazer merge com `max` por hora, **nunca
`sum`**. Alternativa mais simples se quiser menos código: filtrar por
`record.timeline_stream()`. Preferir `max` porque degrada melhor quando as fronteiras de
segmento diferem.

### 6.3 Bug pré-existente encontrado (fora de escopo)

`frigate/util/camera_cleanup.py:115` monta `os.path.join(RECORD_DIR, camera_name)`, mas o
layout real é `RECORD_DIR/%Y-%m-%d/%H/{camera}`. Esse caminho nunca existe — ou seja,
"deletar câmera" nunca removeu arquivos de gravação do disco; depende do delete no DB (linha
52) mais o `sync_recordings`. **Independente deste recurso.** Deixar fora de escopo ou
corrigir como drive-by rotulado, mas não misturar no mesmo commit.

---

## 7. Retenção e storage

### 7.1 `expire_recordings` (`cleanup.py:283`)

Varredura de câmeras removidas (linhas 289-291): `expire_days = max(record.continuous.days,
record.motion.days, record.secondary.continuous.days, record.secondary.motion.days)`.

O loop por câmera (328-367) reestrutura para:

```python
for camera, config in self.config.cameras.items():
    now = datetime.datetime.now()
    maybe_empty_dirs |= self.expire_review_segments(config, now)

    windows = {}   # stream -> (continuous_expire_date, motion_expire_date)
    for stream in RecordStreamEnum:
        cont, mot = config.record.get_retention(stream)
        c_date = (now - timedelta(days=cont.days)).timestamp()
        m_date = (now - timedelta(days=max(mot.days, cont.days))).timestamp()
        windows[stream] = (c_date, m_date)

    # reviews têm que cobrir o stream de vida mais longa
    review_bound = max(c for c, _ in windows.values())
    reviews = (...  ReviewSegment.start_time < review_bound  ...)

    kept_all: list[tuple[float, float]] = []
    for stream, (c_date, m_date) in windows.items():
        dirs, kept = self.expire_camera_stream_recordings(
            c_date, m_date, config, stream, reviews
        )
        maybe_empty_dirs |= dirs
        kept_all.extend(kept)

    maybe_empty_dirs |= self.expire_camera_previews(config, sorted(kept_all))
```

Duas sutilezas que um "só rode duas vezes" ingênuo erraria:

- **O bound de reviews tem que ser `max` entre streams.** Hoje é
  `ReviewSegment.start_time < continuous_expire_date` (linha 358), com um comentário
  explicando que gravações candidatas se estendem até essa data. Com primary continuous=0
  (bound = *agora*) e secondary continuous=60 (bound = 60 dias atrás), usar o bound do
  primary buscaria reviews da janela errada e a passada de expiração do secundário não
  acharia review sobreposto → apagaria segmentos que sobrepõem alertas recentes. `max` é a
  direção segura.
- **Previews têm que expirar uma vez por câmera, não uma por stream.**
  `expire_existing_camera_recordings` hoje faz gravações *e* previews na mesma função
  (linhas 219-279), usando o `kept_recordings` daquela passada. Rodar duas vezes apagaria,
  na primeira passada, previews que as gravações mantidas da segunda salvariam. Daí a
  divisão em `expire_camera_stream_recordings` (retorna `(dirs, kept)`) e
  `expire_camera_previews(config, kept_all)`. **Essa divisão é o maior diff em cleanup.py e
  o ponto onde concentrar a revisão.**

A query de gravações dentro de `expire_camera_stream_recordings` ganha
`& (Recordings.stream == stream.value)`; a lógica de retain mode (191-201) não muda e usa os
modos de alerts/detections do nível da câmera para os dois streams.

Manter um wrapper fino `expire_existing_camera_recordings(...)` delegando ao par novo, ou
renomear de vez e atualizar os testes — preferir renomear, é interno.

### 7.2 `storage.py` — ordem de deleção: **alta resolução primeiro**

A premissa do recurso é que a timeline 24x7 de baixa resolução é o ativo que não se quer
perder; a alta resolução é um bônus em torno de eventos. Sob pressão de disco, o sacrifício
correto é a alta resolução.

Reestruturar `reduce_storage_consumption` em três passadas ordenadas sobre o mesmo corpo de
loop, cada uma parando quando `deleted_segments_size > hourly_bandwidth`:

1. `stream == primary`, sem sobrepor evento `retain_indefinitely` — mais antigo primeiro.
2. `stream == secondary`, sem sobrepor evento retido — mais antigo primeiro.
3. Tudo, incluindo retidos (o fallback "must delete retained" existente em 198-226), ainda
   primary antes de secondary.

Bônus prático: segmentos primary tipicamente têm 5-10× o tamanho dos secondary, então a
passada 1 sozinha normalmente já libera a hora e a 2 nunca roda.

Extrair o corpo compartilhado (o scan de eventos retidos em 162-186 mais o unlink) para
`_delete_pass(query, retained_events, budget) -> (size, deleted)` em vez de triplicá-lo.

### 7.3 `calculate_camera_bandwidth` (`storage.py:45-67`) — bug real

Verificado: a função faz `AVG` de `segment_size/(end-start)` sobre os **últimos 100
segmentos** de uma câmera. Com dois streams intercalados, os "últimos 100" são uma mistura
~50/50, então a estimativa de MB/hr cai entre os dois valores e
`check_storage_needs_cleanup` (linha 118) compara espaço livre contra um número que não é
nem um nem outro.

Correção: calcular a média dos últimos 100 por `(camera, stream)` e armazenar
`self.camera_storage_stats[camera]["bandwidth"] = sum(per_stream)`. A heurística
`needs_refresh` de `< 50 segmentos` também deve ser por stream, senão a estimativa do
secundário congela cedo enquanto o primary ainda está estabilizando.

---

## 8. API de reprodução

### 8.1 Formato de URL — segmento de path, confirmado

**A query string não funciona para o VOD.** Verificado em
`docker/main/rootfs/usr/local/nginx/conf/nginx.conf`: `vod_upstream_location /api` (linha 74)
sem nenhum `vod_upstream_extra_args` em lugar algum de
`docker/main/rootfs/usr/local/nginx/conf/`. O nginx-vod-module só anexa query args ao
request de mapping quando `vod_upstream_extra_args` está setado; além disso
`secure_token $args` (linha 117) consome `$args` para fins de token, e `vod_mapping_cache`
indexa pela URI mapeada. **O stream tem que ser um segmento de path.**

| propósito | rota |
|---|---|
| existente (primary, inalterada) | `/vod/{camera_name}/start/{start_ts}/end/{end_ts}` |
| **nova** | `/vod/{camera_name}/stream/{stream}/start/{start_ts}/end/{end_ts}` |
| clip existente (primary) | `/vod/clip/{camera_name}/start/{a}/end/{b}` |
| **novo** clip | `/vod/clip/{camera_name}/stream/{stream}/start/{a}/end/{b}` |
| hora, sem tz | `/vod/{year_month}/{day}/{hour}/{camera_name}` (inalterada) |
| hora, com tz | `/vod/{year_month}/{day}/{hour}/{camera_name}/{tz_name}` (inalterada) |
| evento | `/vod/event/{event_id}` (inalterada — sempre primary) |

A implementação é pequena: `vod_ts` ganha `stream: RecordStreamEnum =
RecordStreamEnum.primary` e passa para `camera_range(...)`; as rotas novas são wrappers
finos:

```python
@router.get(
    "/vod/{camera_name}/stream/{stream}/start/{start_ts}/end/{end_ts}",
    dependencies=[Depends(require_camera_access)],
)
async def vod_ts_stream(
    camera_name: str,
    stream: RecordStreamEnum,
    start_ts: float,
    end_ts: float,
    force_discontinuity: bool = False,
):
    return await vod_ts(camera_name, start_ts, end_ts, force_discontinuity, stream)
```

**A ordem de rotas é inequívoca** — as novas têm 6 segmentos de path contra 5 das legadas, e
o literal `start` fica em posição diferente, então o FastAPI não consegue casar errado. Usar
um path param tipado como `RecordStreamEnum` dá rejeição 422 grátis para lixo.

O nginx **não precisa de mudança**: o browser pede
`.../stream/secondary/start/A/end/B/master.m3u8`, o nginx-vod tira o nome de arquivo final e
emite o request de mapping para `/api/vod/{camera}/stream/secondary/start/A/end/B`, que o
`location /api/vod/` (nginx.conf:263) já proxia para `/vod/...`.

### 8.2 Implicações de auth

- `/vod/` está em `EXEMPT_PREFIXES` (`auth.py:130`), então as rotas novas contornam o gate
  global de admin exatamente como as existentes — **nenhuma mudança na lista de isenções é
  necessária, e nenhuma deve ser feita.**
- A autorização por câmera continua vindo do `Depends(require_camera_access)` no nível da
  rota, que lê `camera_name` do path. Como `camera_name` continua sendo o **primeiro** path
  param nas rotas novas, `require_camera_access` resolve identicamente. É por isso que
  `stream` vem *depois* da câmera: `/vod/stream/{s}/{camera}/...` ainda funcionaria via
  dependency, mas quebraria a isenção dinâmica "primeiro segmento é nome de câmera"
  (`auth.py:135-146`) para quem chegasse nesses paths sem o prefixo `/vod/`, p.ex. via
  rewrites de `base_path`.
- Teste de regressão em `frigate/test/http_api/`: usuário viewer com acesso a `cam_a` recebe
  403 em `/vod/cam_b/stream/secondary/start/0/end/1`.

### 8.3 `/{camera}/recordings`

Adicionar `stream: RecordStreamEnum = RecordStreamEnum.primary` como query param em
`api/record.py:227` e ao `RecordingsQueryParams` em
`frigate/api/defs/query/recordings_query_parameters.py`. Query param (não path) é adequado
aqui — esse endpoint é chamado direto pela SPA via `/api/`, nunca através do nginx-vod.

Retornar também `stream` no dict da linha, para o frontend poder afirmar o que recebeu.

Ao terminar: `python3 generate_api_auth_spec.py` (o CI falha com o `--check` se
desatualizado).

---

## 9. Export

`RecordingExporter` (`frigate/record/export.py`) ganha `stream: RecordStreamEnum` no
construtor (default primary), propagado de:

- o endpoint de início de export em `frigate/api/export.py` e o modelo `BatchExportItem`
  (`frigate/api/defs/request/export_body.py` — adicionar `stream: RecordStreamEnum =
  primary`);
- `_validate_export_source` (linha 160) e `_get_item_recording_export_errors` (linha 228),
  que recebem o stream e o passam para `camera_range`.

Em `get_record_export_command` (linha 686):
- a query paginada ganha a cláusula de stream;
- os dois construtores de URL viram
  `http://127.0.0.1:{port}/vod/{camera}/stream/{stream.value}/start/{a}/end/{b}/index.m3u8`.

Como o export reconsome o próprio endpoint de VOD via localhost, tornar o VOD ciente de
stream realmente entrega a muxagem de graça — o único trabalho é a URL e a query paginada.
Dois pontos a verificar na implementação:
- O request de localhost vai para a porta **interna**, ou seja, direto para o app FastAPI,
  **não** pelo nginx-vod. Então ele bate em `vod_ts_stream` e retorna o JSON de mapping — mas
  o ffmpeg recebe `index.m3u8`. Confirmar que o listener interno serve HLS para esse path
  (deve servir hoje, já que o código existente faz o mesmo com `index.m3u8`) e que os
  segmentos de path extras não o perturbam.
- `_build_recording_segment_chapter_metadata_file` / `_build_chapter_metadata_file` consomem
  a mesma lista `recordings`, então os capítulos seguem o stream selecionado
  automaticamente. Capítulos de review item são independentes de stream e não mudam.

Metadata do arquivo exportado: anexar o stream ao `comment`
(`comment=Camera: {camera}` → `, Stream: {stream}`) para que um arquivo de baixa resolução
seja identificável depois.

---

## 10. Frontend

### 10.1 Caminho de plumbing

```
RecordingView.tsx
  useState<RecordStream>("primary")            ──┐
  (persistir por câmera com usePersistence:      │
   "record-stream-{camera}")                     │ stream, onStreamChange
                                                 ▼
DynamicVideoPlayer.tsx
  recordingParams memo → SWR key [`${camera}/recordings`, {after, before, stream}]
  setSource({ playlist:
    `${apiHost}vod/${camera}/stream/${stream}/start/${after}/end/${before}/master.m3u8` })
                                                 │ availableStreams, stream, onSetStream
                                                 ▼
HlsVideoPlayer.tsx   (pass-through puro)
                                                 ▼
VideoControls.tsx    features.recordStream → DropdownMenu/RadioGroup
```

Detalhe crítico em `DynamicVideoPlayer.tsx`: `stream` tem que entrar no **memo
`recordingParams`** (linhas 234-241), não só no template da URL. Esse memo é a chave do SWR
*e* é do que o `useEffect` de 245-277 depende via `recordings`. Colocando ali, a troca de
stream refaz o fetch das gravações, recalcula `startPosition` via
`calculateInpointOffset`/`calculateSeekPosition` e reconstrói a playlist numa passada
atômica — que é exatamente o que se quer, porque as fronteiras de segmento dos dois streams
diferem e reusar os offsets antigos faria seek no lugar errado.

Para preservar o playhead na troca, capturar `controller.getProgress(player.currentTime)`
antes da mudança e passar como novo `startTimestamp`. `DynamicVideoController` mapeia
timestamp ↔ tempo do player e não precisa de mudança — ele já recebe um array `recordings`
novo a cada `newPlayback`.

`setSource` na linha 273 é de fato o único lugar onde a URL da playlist é montada.

### 10.2 UI do seletor

Em `VideoControls.tsx`, ao lado do dropdown de `playbackRate` (linhas 254-282), com um flag
novo `recordStream` no tipo `VideoControls` (linha 40) e em `CONTROLS_DEFAULT` (linha 49):

```tsx
{features.recordStream && availableStreams.length > 1 && (
  <DropdownMenu onOpenChange={/* ...igual ao playbackRate... */}>
    <DropdownMenuTrigger>{t(`stream.${selectedStream}.short`)}</DropdownMenuTrigger>
    <DropdownMenuContent
      portalProps={{
        container: containerRef?.current ?? controlsContainerRef.current,
      }}
    >
      <DropdownMenuRadioGroup value={selectedStream} onValueChange={onSetStream}>
        {availableStreams.map((s) => (
          <DropdownMenuRadioItem key={s} value={s} className="cursor-pointer">
            {t(`stream.${s}.label`)}
          </DropdownMenuRadioItem>
        ))}
      </DropdownMenuRadioGroup>
    </DropdownMenuContent>
  </DropdownMenu>
)}
```

`availableStreams` deriva do config:
`config.cameras[camera].record.secondary?.enabled ? ["primary","secondary"] : ["primary"]`.
Com um stream só o controle não renderiza — **instalações single-stream veem zero mudança
de UI.**

Tipos: adicionar `secondary?: { enabled: boolean; continuous: {days:number};
motion: {days:number} }` ao shape de record em `web/src/types/frigateConfig.ts`; adicionar
`stream?: "primary" | "secondary"` a `Recording` em `web/src/types/record.ts`.

i18n (obrigatório, sem literais) — `web/public/locales/en/components/player.json`:
```json
{
  "stream": {
    "label": "Stream",
    "primary":   { "label": "High resolution", "short": "HD" },
    "secondary": { "label": "Continuous (low resolution)", "short": "SD" }
  }
}
```

### 10.3 Indicação de HD na timeline — sem endpoint novo

`RecordingView.tsx:1122` já busca `recordings/unavailable` para o sombreamento de lacunas da
timeline. Adicionar uma **segunda** chamada SWR com os mesmos params mais
`stream: "primary"`. Ela retorna "faixas sem alta resolução", que é precisamente o inverso
do que se quer mostrar.

Passar para `MotionReviewTimeline` como um prop novo `lowResOnlyRanges` e renderizar em
`VirtualizedMotionSegments`/`MotionSegment.tsx` do mesmo modo que `noRecordingRanges`, mas
com tratamento distinto e mais sutil (hachura diagonal ou overlay de opacidade reduzida, em
vez do preenchimento sólido de "sem gravação"). String de legenda/tooltip via i18n.

Duas razões para esse formato:
- Zero superfície nova de backend — é o endpoint existente com um param novo, e o param tem
  default igual ao comportamento de hoje, então nada mais muda.
- É exatamente o dado que a fase adiada de **fallback automático por segmento** precisa.
  Quando ela chegar, o player consulta as mesmas faixas para decidir, por segmento, se pede
  o primary ou cai para o secundário — sem API nova, sem mudança no modelo de storage.
  O requisito "não pode impedir o fallback depois" fica satisfeito estruturalmente, não por
  promessa.

### 10.4 Regeneração obrigatória

- `python3 generate_config_translations.py` — os campos Pydantic novos geram a UI de
  configuração. **Nunca editar `web/public/locales/en/config/*.json` à mão.**
- `npm run i18n:extract` (e `npm run i18n:extract:ci` para verificar).
- `python3 generate_api_auth_spec.py`.

---

## 11. Testes

Convenção do repo: `python3 -u -m unittest frigate.test.<module>`.

**Novo `frigate/test/test_record_streams.py`**
- `parse_cache_filename` round-trip: `cam@2026...` → (cam, primary);
  `cam#secondary@2026...` → (cam, secondary); `front-door_2#secondary@...`
  (hífen/underscore/dígito no nome); `cam#bogus@...` → `None`; `garbage` → `None`.
- `cache_segment_prefix` é o inverso exato.
- Tabela-verdade de `RecordConfig.get_retention` / `stream_enabled` / `enabled_streams` /
  `timeline_stream`, incluindo `record.enabled: false` mascarando `secondary.enabled: true`.

**Adições a `frigate/test/test_config.py`**
- dois inputs `[record, detect]` + `[record_secondary]` → dois comandos ffmpeg, alvos de
  cache distintos, `pipe:` por último no cmd de detect.
- `record_secondary` sem `record` → ValueError.
- `record_secondary` duas vezes → ValueError existente de "cada role uma vez".
- input único + `record.secondary.enabled: true` → ValueError vinda do
  `CameraConfig.__init__` (garante que a guarda dispara *antes* da sobrescrita de roles).
- `record.secondary.enabled: true` sem o role → ValueError de `verify_config_roles`.
- `output_args.record_secondary` custom com `-segment_time 3600` → ValueError da checagem
  estendida.
- **regressão**: config de input único existente continua recebendo `["record","detect"]` e
  um comando só.

**Adições a `frigate/test/test_maintainer.py`**
- dois streams no cache → `grouped_recordings` tem duas chaves para uma câmera.
- `MAX_SEGMENTS_IN_CACHE` aplicado por `(camera, stream)`: semear 8 primary + 8 secondary,
  afirmar que 6 de cada sobrevivem (**este teste falha numa implementação chaveada só por
  câmera**).
- 🔴 **regressão do pop-loop**: semear `object_recordings_info[cam]` cobrindo os dois
  streams, fazer o segmento mais antigo do primary ser *mais novo* que o do secondary, rodar
  `move_files`, e afirmar que o `segment_stats` do secondary ainda enxerga seus frames de
  movimento. **É o teste de maior valor do conjunto.**
- `move_segment` grava em `.../{camera}/secondary/%M.%S.mp4` e retorna
  `Recordings.stream == "secondary"`.
- `_expire_stale_recordings_info` com chaves compostas descarta só câmeras ausentes de
  *todos* os streams.
- seleção de retenção: secondary com `continuous.days=30` mantém um segmento sem movimento
  que o primary (`continuous.days=0`) descarta, na mesma chamada de `move_files`.

**Novo `frigate/test/test_record_cleanup.py`**
- datas de expiração por stream aplicadas às linhas certas.
- o bound de reviews usa `max` entre streams (montar primary=0/secondary=60 e afirmar que um
  segmento secondary de 3 dias sobrepondo um alerta de 3 dias sobrevive).
- previews expiram uma vez por câmera usando as gravações mantidas dos dois streams.

**Adições a `frigate/test/test_storage.py`**
- `reduce_storage_consumption` apaga todas as linhas primary elegíveis antes de tocar
  qualquer secondary.
- `calculate_camera_bandwidth` com mistura 50/50 de segmentos de 1 MB e 10 MB retorna a
  *soma* das taxas por stream, não a média misturada.

**Adições a `frigate/test/test_camera_maintainer.py`**
- desempacotamento do payload de 4 elementos.
- um secondary parado com primary saudável reinicia exatamente o processo secundário
  (afirmar `start_or_restart_ffmpeg` chamado uma vez, com o cmd do secundário).
- `{camera}/status/record` continua `online` enquanto `{camera}/status/record_secondary` vai
  para `offline`.

**`frigate/test/http_api/`**
- `/vod/{cam}/stream/secondary/start/A/end/B` retorna só paths secondary; a legada
  `/vod/{cam}/start/A/end/B` retorna só primary, dado um DB com os dois.
- `/{cam}/recordings?stream=secondary` retorna só linhas secondary; sem param → só primary.
- auth: viewer restrito a `cam_a` recebe 403 na rota nova de `cam_b` (estende
  `test_media_auth.py`).

**E2E manual**
1. Configurar o YAML alvo numa câmera; subir; confirmar nos logs `frigate.video` dois
   processos ffmpeg e `ls /tmp/cache` com os dois padrões (`cam@…` e `cam#secondary@…`).
2. Após 2 minutos: `ls /media/frigate/recordings/$(date -u +%F)/$(date -u +%H)/cam/` mostra
   `MM.SS.mp4` e o subdiretório `secondary/`;
   `sqlite3 frigate.db "select stream, count(*) from recordings group by stream"` mostra os
   dois.
3. Provocar movimento; esperar passar a janela de cache do primary; confirmar que segmentos
   primary só existem em torno do review item enquanto os secondary são contínuos.
4. Browser: abrir History, confirmar que o seletor HD/SD aparece, alternar, confirmar no
   devtools que a URL da playlist muda e que a reprodução continua perto do mesmo timestamp.
5. Confirmar que a timeline hachura as faixas só-baixa-resolução e que elas coincidem com
   onde o primary não tem segmentos.
6. `kill -9` **apenas** no PID do ffmpeg secundário; confirmar que o log reinicia exatamente
   esse processo em ~2× segment_time e que o primary não é tocado.
7. Exportar uma faixa com `stream=secondary`; `ffprobe` no resultado e confirmar a baixa
   resolução.
8. Baixar `secondary.continuous.days` para um valor mínimo, forçar `expire_recordings`,
   confirmar que só linhas/arquivos secondary desaparecem.
9. **Teste de upgrade**: partir de um build pré-mudança com dados, atualizar, confirmar que a
   migration 036 roda, que todas as linhas antigas leem `primary`, que a reprodução de
   gravações antigas não muda e que `sync_recordings` reporta zero órfãos.

Lint/format ao final: `ruff format frigate/`, `ruff check frigate/`,
`python3 -u -m mypy --config-file frigate/mypy.ini frigate`, `npm run lint` em `web/`.

---

## 12. Fases e riscos

### Fases

**P0 — Fundação, zero mudança de comportamento.**
`frigate/record/types.py`; `frigate/record/queries.py`; migration 036 + `Recordings.stream`;
refatorar os ~26 sites de query para o helper compartilhado, cada um passando
`RecordStreamEnum.primary` ou `None` **explicitamente**.
*Checkpoint:* suíte completa verde; instância rodando com comportamento idêntico; DB com a
coluna, todas as linhas `primary`.
Esta fase é deliberadamente grande e chata. É também onde a equivalência entre o OR de 3
cláusulas antigo e o `overlaps()` novo fica provada, isoladamente, antes de existir qualquer
dado de dois streams para confundir um bisect.

**P1 — Config + ffmpeg, sem consumo em runtime ainda.**
Enum de role, `RecordSecondaryConfig`, validadores, `_get_ffmpeg_cmd`, helpers de nome de
cache, `get_record_segment_time(stream)`.
*Checkpoint:* o YAML alvo carrega; `camera_config.ffmpeg_cmds` contém dois comandos com
alvos de cache distintos; uma execução manual produz arquivos `cam#secondary@….mp4` em
`/tmp/cache` (que o maintainer simplesmente ignora com um warning até a P2 — inofensivo).

**P2 — Pipeline.** Publisher de 4 elementos + os dois consumidores; dicts por stream no
watchdog; parsing/agrupamento/pop-loop/retenção/`move_segment` no maintainer; tratamento do
processo detect que carrega role de record (risco #2).
*Checkpoint:* os dois streams caem nos diretórios certos com os valores de `stream` certos;
o teste de regressão do pop-loop passa; matar um ffmpeg reinicia só aquele.

**P3 — Retenção + storage.** Reestruturação de cleanup.py; bandwidth por stream e deleção
primary-first em storage.py.
*Checkpoint:* dias de retenção por stream honrados independentemente; pressão de disco
simulada remove HD antes de SD.

**P4 — API de reprodução + export.** Rotas novas de VOD; `?stream=` em
`/{camera}/recordings`, `/recordings/unavailable`, resumo horário; filtro de timeline-spine
em `/review/activity/motion`; export.
*Checkpoint:* as duas URLs de VOD tocam no browser; export com `stream=secondary` produz um
arquivo verificavelmente de baixa resolução.

**P5 — Frontend.** Tipos, plumbing, seletor, indicação na timeline, i18n.
*Checkpoint:* o seletor aparece só quando configurado; a hachura da timeline coincide com a
cobertura do primary.

**P6 — Docs.** `docs/docs/configuration/record.md`, a referência completa de config,
`docs/docs/integrations/mqtt.md` para `{camera}/status/record_secondary`, e uma receita
"dual-stream estilo Blue Iris".

P0–P2 são estritamente sequenciais. P3/P4 podem ir em paralelo depois da P2. P5 depende da
P4.

### Riscos, ordenados por (probabilidade × silêncio)

1. **A mudança de aridade do `RecordingsDataPublisher`.** Um consumidor esquecido mata o
   loop de drenagem do `CameraWatchdog` com `ValueError` de unpack, e o monitoramento de
   saúde de gravação para para *todas* as câmeras sem log apontando a causa. São
   exatamente dois consumidores (verificado): `video/ffmpeg.py:162` e
   `embeddings/maintainer.py:153`. Mitigar renomeando o método de publish para que callers
   defasados falhem alto.
2. **O processo ffmpeg que carrega `detect` + `record_secondary`** é gerenciado por
   `start_ffmpeg_detect()` (`video/ffmpeg.py:533`) e **não** entra em
   `ffmpeg_other_processes` — escapando do monitor de saúde de gravação. Espelha uma
   limitação que já existe hoje no caso de input único, mas aqui passa a valer para o stream
   24x7, que é justamente o que não se pode perder. Tratar explicitamente na P2.
3. **Os loops de `pop` em `object_recordings_info` (`maintainer.py:253-267`).** Perda de
   metadados de movimento dependente de timing, não determinística, no stream que estiver
   atrasado. Manifesta-se dias depois como segmentos expirados indevidamente. Só alcançável
   com dois streams, então não quebra instalações single-stream — mas vai parecer um bug
   fantasma em campo. Testar explicitamente.
4. **Qualquer site de query onde o filtro de stream seja esquecido** — linhas 2, 3, 4, 8,
   17, 18, 22 de §6.2. Mitigação estrutural: `camera_range(..., stream)` **sem default**,
   para que todo call site seja uma decisão consciente. Não adicionar default "por
   conveniência".
5. **A sobrescrita de roles de input único (`camera.py:217-226`).** Roda antes da validação e
   destrói a evidência. A guarda tem que estar *dentro* do `__init__`.
6. **Ponto cego do `verify_recording_segments_setup_with_reasonable_time`.** Um
   `-segment_time` grande em `record_secondary` estoura `MAX_SEGMENTS_IN_CACHE` e o
   `/tmp/cache` silenciosamente.
7. **`storage.calculate_camera_bandwidth` com médias misturadas.** Afeta o threshold de
   espaço livre, então pode causar tanto deleção prematura de dado bom quanto disco cheio de
   verdade. Silencioso nas duas direções.
8. **O bound de reviews e a passada dupla de previews em `expire_recordings`.** Os dois
   apagam dados, os dois em silêncio. O `max` do bound e a divisão de previews são os dois
   requisitos de corretude mais sutis de §7.
9. **A criação do índice composto num DB grande.** Não é risco de corretude, mas é uma
   parada visível no upgrade; adicionar log antes/depois na migration.
10. **`MAX_SEGMENTS_IN_CACHE` e dimensionamento do `/tmp/cache`.** Dois streams dobram o
    churn de cache e `/tmp/cache` costuma ser tmpfs. A doc precisa dizer que dual-stream
    aproximadamente dobra a necessidade de RAM/disco de cache.
11. **Exports e links compartilhados existentes** montados contra o formato legado de URL de
    VOD têm que continuar funcionando. Continuam — as rotas legadas ficam intactas e caem em
    primary — mas não ceder à tentação de "unificá-las" no formato `/stream/{s}/` com
    redirect.
12. **`Recordings.id` com `max_length=30`.** Resistir a codificar o stream no id.
13. **`SAFETY_THRESHOLD` do `sync_recordings` durante o rollout da P2.** Rodar um build P1
    (que escreve arquivos de cache secondary) contra um DB P0 é inofensivo (o maintainer
    avisa e ignora); mas rodar um build P2 e depois **voltar** para P0 transforma os arquivos
    secondary em disco em órfãos, e o sync vai apagá-los. Registrar essa porta de mão única
    nas notas de release.
