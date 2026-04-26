/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        // Text / ink scale (dark, warm)
        ink: {
          900: "#2F2A24",
          800: "#3F3830",
          700: "#5B5346",
          600: "#7A6E5A",
          500: "#9B8E75",
        },
        // Paper / surface scale (warm whites and beiges)
        paper: {
          900: "#FFFDF8",
          800: "#FFFFFF",
          700: "#FAF5E8",
          600: "#F1E6CF",
          500: "#E4D6B0",
        },
        // Primary accent — brand orange (#e8640c and scale)
        accent: {
          300: "#fcd4b8",
          400: "#e8640c",
          500: "#cf5a0a",
          600: "#b34d09",
          700: "#8f3e07",
        },
        // Severity palette echoed for utility classes
        severity: {
          destroyed: "#D95D39",
          severe: "#F28C28",
          moderate: "#e8943d",
          minor: "#f4b97a",
          unknown: "#9CA3AF",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "ui-sans-serif",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "monospace"],
      },
      boxShadow: {
        soft: "0 1px 2px rgba(47, 42, 36, 0.04), 0 4px 14px rgba(47, 42, 36, 0.06)",
        card: "0 1px 0 rgba(47, 42, 36, 0.04), 0 6px 24px rgba(47, 42, 36, 0.06)",
      },
    },
  },
  plugins: [],
};
