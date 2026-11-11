import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { RecordStream } from "@/types/record";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Label } from "../ui/label";
import { RadioGroup, RadioGroupItem } from "../ui/radio-group";

type ExportStreamFieldProps = {
  idPrefix: string;
  value: RecordStream;
  onChange: (stream: RecordStream) => void;
  showMixedCamerasNote?: boolean;
};

export function ExportStreamField({
  idPrefix,
  value,
  onChange,
  showMixedCamerasNote = false,
}: ExportStreamFieldProps) {
  const { t } = useTranslation(["components/dialog"]);

  const options: {
    stream: RecordStream;
    label: string;
    description: string;
  }[] = [
    {
      stream: "secondary",
      label: t("export.quality.sd"),
      description: t("export.quality.sdDescription"),
    },
    {
      stream: "primary",
      label: t("export.quality.hd"),
      description: t("export.quality.hdDescription"),
    },
  ];

  return (
    <div className="space-y-2">
      <div className="space-y-1">
        <Label className="text-sm text-primary">
          {t("export.quality.label")}
        </Label>
        <p className="text-xs text-muted-foreground">
          {t("export.quality.description")}
        </p>
      </div>
      <RadioGroup
        aria-label={t("export.quality.label")}
        className="flex flex-col gap-3"
        value={value}
        onValueChange={(next) => onChange(next as RecordStream)}
      >
        {options.map((option) => (
          <div key={option.stream} className="flex items-start gap-2">
            <RadioGroupItem
              className={
                value === option.stream
                  ? "mt-0.5 bg-selected from-selected/50 to-selected/90 text-selected"
                  : "mt-0.5 bg-secondary from-secondary/50 to-secondary/90 text-secondary"
              }
              id={`${idPrefix}-${option.stream}`}
              value={option.stream}
            />
            <div className="space-y-0.5">
              <Label
                className="cursor-pointer"
                htmlFor={`${idPrefix}-${option.stream}`}
              >
                {option.label}
              </Label>
              <p className="text-xs text-muted-foreground">
                {option.description}
              </p>
            </div>
          </div>
        ))}
      </RadioGroup>
      {showMixedCamerasNote && value === "secondary" && (
        <p className="text-xs text-muted-foreground">
          {t("export.quality.mixedCameras")}
        </p>
      )}
    </div>
  );
}

type ExportStreamPromptProps = {
  open: boolean;
  isStarting?: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: (stream: RecordStream) => void;
};

/**
 * Asks SD vs HD before a one-click export. Defaults to the continuous
 * stream so confirming does not join only the gapped HD segments.
 */
export function ExportStreamPrompt({
  open,
  isStarting = false,
  onOpenChange,
  onConfirm,
}: ExportStreamPromptProps) {
  const { t } = useTranslation(["components/dialog", "common"]);
  const [stream, setStream] = useState<RecordStream>("secondary");

  useEffect(() => {
    if (open) {
      setStream("secondary");
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t("export.quality.promptTitle")}</DialogTitle>
          <DialogDescription className="sr-only">
            {t("export.quality.description")}
          </DialogDescription>
        </DialogHeader>
        <ExportStreamField
          idPrefix="export-stream-prompt"
          value={stream}
          onChange={setStream}
        />
        <DialogFooter>
          <Button
            aria-label={t("button.cancel", { ns: "common" })}
            disabled={isStarting}
            onClick={() => onOpenChange(false)}
          >
            {t("button.cancel", { ns: "common" })}
          </Button>
          <Button
            aria-label={t("export.export")}
            variant="select"
            disabled={isStarting}
            onClick={() => onConfirm(stream)}
          >
            {isStarting ? t("export.queueing") : t("export.export")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
