// Minimal subset of the Telegram WebApp API surface we actually use.
// Full reference: https://core.telegram.org/bots/webapps

export interface TelegramThemeParams {
  bg_color?: string;
  text_color?: string;
  hint_color?: string;
  link_color?: string;
  button_color?: string;
  button_text_color?: string;
  secondary_bg_color?: string;
}

export type InvoiceStatus = "paid" | "cancelled" | "failed" | "pending";

export interface TelegramWebApp {
  initData: string;
  themeParams: TelegramThemeParams;
  colorScheme: "light" | "dark";
  expand(): void;
  ready(): void;
  onEvent(event: string, cb: () => void): void;
  openInvoice(url: string, callback: (status: InvoiceStatus) => void): void;
  HapticFeedback?: {
    notificationOccurred(type: "error" | "success" | "warning"): void;
  };
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp };
  }
}

export function getTg(): TelegramWebApp | null {
  return window.Telegram?.WebApp ?? null;
}

export function applyTheme(tg: TelegramWebApp): void {
  const t = tg.themeParams;
  const root = document.documentElement;
  const set = (k: string, v: string | undefined) => {
    if (v) root.style.setProperty(k, v);
  };
  set("--tg-bg", t.bg_color);
  set("--tg-text", t.text_color);
  set("--tg-hint", t.hint_color);
  set("--tg-link", t.link_color);
  set("--tg-button", t.button_color);
  set("--tg-button-text", t.button_text_color);
  set("--tg-secondary-bg", t.secondary_bg_color);
}
