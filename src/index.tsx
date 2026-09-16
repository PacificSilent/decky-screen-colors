import {
  ButtonItem,
  DialogButton,
  Field,
  Focusable,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { FC, useEffect, useRef, useState } from "react";
import { FaPalette } from "react-icons/fa";

import { t } from "./i18n";

// ---- RPC bridge (names must match the async def methods on the Python Plugin)
type Color = Record<string, number>;
interface State {
  supported: boolean;
  detail: string;
  color: Color;
  ranges: Record<string, [number, number]>;
  native: Color;
  presets: string[];
  active: string;
}

const getState = callable<[], State>("get_state");
const setColor = callable<[fields: Color], State>("set_color");
const applyPreset = callable<[key: string], State>("apply_preset");
const applyOledLook = callable<[], State>("apply_oled_look");
const resetColor = callable<[], State>("reset");

// Dragging a slider fires a change per step; each one rebuilds a 17^3 LUT on the
// device. Coalesce the changes and push them once the stick settles.
const APPLY_DELAY_MS = 120;

// [field, i18n key, step]
const MAIN: [string, string, number][] = [
  ["saturation", "field.saturation", 5],
  ["vibrance", "field.vibrance", 5],
  ["temperature", "field.temperature", 5],
  ["contrast", "field.contrast", 2],
  ["gamma", "field.gamma", 5],
  ["black", "field.black", 5],
];
const ADVANCED: [string, string, number][] = [
  ["hue", "field.hue", 5],
  ["gain_r", "field.gain_r", 2],
  ["gain_g", "field.gain_g", 2],
  ["gain_b", "field.gain_b", 2],
];

/** Human-readable name of the look that is currently applied. */
const activeLabel = (active: string) =>
  active === "native" || active === "custom" || active === "oled"
    ? t(`active.${active}`)
    : t(`preset.${active}`);

const ColorPanel: FC = () => {
  const [state, setState] = useState<State | null>(null);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const pending = useRef<Color>({});
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = () => {
    timer.current = null;
    const fields = pending.current;
    pending.current = {};
    if (!Object.keys(fields).length) return;
    // Keep whatever the user moved while the call was in flight; the backend is
    // authoritative for everything else (active look, support status).
    setColor(fields)
      .then((s) => setState({ ...s, color: { ...s.color, ...pending.current } }))
      .catch(() => undefined);
  };

  useEffect(() => {
    getState().then(setState).catch(() => setState(null));
    return () => {
      // The panel unmounts when the QAM closes: don't drop an unsent tweak.
      if (timer.current) clearTimeout(timer.current);
      flush();
    };
  }, []);

  // A preset/reset replaces the whole state, so drop any tweak still queued.
  const run = async (call: () => Promise<State>) => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
    pending.current = {};
    setState(await call());
  };

  if (!state) {
    return (
      <PanelSection title={t("title")} spinner>
        <PanelSectionRow>{t("loading")}</PanelSectionRow>
      </PanelSection>
    );
  }

  if (!state.supported) {
    return (
      <PanelSection title={t("title")}>
        <PanelSectionRow>{t("unsupported.body")}</PanelSectionRow>
        <PanelSectionRow>
          <span style={{ opacity: 0.6, fontSize: "0.75em" }}>{state.detail}</span>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const change = (f: string, v: number) => {
    setState((s) => (s ? { ...s, color: { ...s.color, [f]: v }, active: "custom" } : s));
    pending.current[f] = v;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(flush, APPLY_DELAY_MS);
  };

  const slider = ([f, key, step]: [string, string, number]) => {
    const [min, max] = state.ranges[f] ?? [0, 100];
    return (
      <PanelSectionRow key={f}>
        <SliderField
          label={t(key)}
          value={state.color[f]}
          min={min}
          max={max}
          step={step}
          resetValue={state.native[f]}
          showValue
          onChange={(v) => change(f, v)}
        />
      </PanelSectionRow>
    );
  };

  return (
    <>
      <PanelSection title={t("section.presets")}>
        <PanelSectionRow>
          <Field label={t("label.active")} childrenLayout="inline" bottomSeparator="thick">
            <span style={{ fontWeight: "bold" }}>{activeLabel(state.active)}</span>
          </Field>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description={t("btn.oledLook.desc")}
            onClick={() => run(applyOledLook)}
          >
            {state.active === "oled" ? `✓ ${t("btn.oledLook")}` : t("btn.oledLook")}
          </ButtonItem>
        </PanelSectionRow>

        <PanelSectionRow>
          <Focusable style={{ display: "flex", gap: "6px" }}>
            {state.presets.map((k) => (
              <DialogButton
                key={k}
                style={{ flex: 1, minWidth: 0, padding: "8px 4px", fontSize: "0.85em" }}
                onClick={() => run(() => applyPreset(k))}
              >
                {state.active === k ? `✓ ${t(`preset.${k}`)}` : t(`preset.${k}`)}
              </DialogButton>
            ))}
          </Focusable>
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={state.active === "native"}
            onClick={() => run(resetColor)}
          >
            {t("btn.reset")}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title={t("section.manual")}>{MAIN.map(slider)}</PanelSection>

      <PanelSection title={t("section.advanced")}>
        <PanelSectionRow>
          <ToggleField
            label={t("toggle.advanced")}
            description={showAdvanced ? undefined : t("toggle.advanced.desc")}
            checked={showAdvanced}
            onChange={setShowAdvanced}
          />
        </PanelSectionRow>
        {showAdvanced && ADVANCED.map(slider)}
      </PanelSection>
    </>
  );
};

export default definePlugin(() => ({
  name: "Screen Colors",
  titleView: <div className={staticClasses.Title}>{t("title")}</div>,
  content: <ColorPanel />,
  icon: <FaPalette />,
}));
