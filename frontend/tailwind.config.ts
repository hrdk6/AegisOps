import type { Config } from "tailwindcss";

const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: ["class", '[data-theme="dark"]'],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: v("bg"),
        panel: v("panel"),
        raised: v("raised"),
        line: v("line"),
        ink: v("ink"),
        ink2: v("ink2"),
        muted: v("muted"),
        accent: v("accent"),
        accent2: v("accent2"),
        accentink: v("accent-ink"),
        good: v("good"),
        warning: v("warning"),
        serious: v("serious"),
        critical: v("critical"),
      },
      fontFamily: {
        sans: ["var(--font-archivo)", "system-ui", "sans-serif"],
        display: ["var(--font-archivo)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "ui-monospace", "monospace"],
      },
      fontSize: {
        xs: ["12px", "16px"],
        sm: ["13px", "18px"],
        base: ["14px", "20px"],
        md: ["16px", "22px"],
        lg: ["20px", "26px"],
        xl: ["26px", "32px"],
        "2xl": ["34px", "38px"],
        "3xl": ["44px", "46px"],
      },
      borderRadius: { sm: "4px", DEFAULT: "6px", lg: "10px", xl: "14px" },
    },
  },
  plugins: [],
} satisfies Config;
