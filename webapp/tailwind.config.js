/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // These are populated at runtime from Telegram themeParams via
        // CSS variables on :root, set in src/main.tsx. Tailwind just sees
        // them as ordinary tokens.
        tg: {
          bg: "var(--tg-bg)",
          text: "var(--tg-text)",
          hint: "var(--tg-hint)",
          link: "var(--tg-link)",
          button: "var(--tg-button)",
          buttonText: "var(--tg-button-text)",
          secondaryBg: "var(--tg-secondary-bg)",
        },
      },
    },
  },
  plugins: [],
};
