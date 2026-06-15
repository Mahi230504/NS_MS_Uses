/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // base surfaces (OLED command-center)
        ink: "#070A12", // app background, deepest
        panel: "#0E1320", // card/panel surface
        panel2: "#141B2B", // raised surface (inputs, nested)
        // brand: cyan = "live agent action", indigo/violet = "Atlas intelligence"
        accent: "#22d3ee", // cyan — keep in sync with DeviceCanvas overlay
        brand: {
          cyan: "#22d3ee",
          sky: "#38bdf8",
          indigo: "#6366f1",
          violet: "#a78bfa",
        },
      },
      fontFamily: {
        display: ['"Space Grotesk"', "Inter", "system-ui", "sans-serif"],
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      backgroundImage: {
        brand: "linear-gradient(120deg, #22d3ee 0%, #38bdf8 35%, #6366f1 75%, #a78bfa 100%)",
        "brand-soft":
          "linear-gradient(120deg, rgba(34,211,238,0.16), rgba(99,102,241,0.16))",
      },
      boxShadow: {
        "glow-cyan": "0 0 0 1px rgba(34,211,238,0.25), 0 8px 40px -12px rgba(34,211,238,0.45)",
        "glow-indigo": "0 0 0 1px rgba(99,102,241,0.25), 0 8px 40px -12px rgba(99,102,241,0.45)",
        "glow-amber": "0 0 0 1px rgba(251,191,36,0.30), 0 8px 44px -12px rgba(251,191,36,0.45)",
        card: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 20px 50px -28px rgba(0,0,0,0.9)",
      },
      keyframes: {
        ping2: {
          "0%": { transform: "scale(1)", opacity: "0.9" },
          "75%, 100%": { transform: "scale(2.4)", opacity: "0" },
        },
        aurora: {
          "0%, 100%": { transform: "translate3d(0,0,0) rotate(0deg)", opacity: "0.55" },
          "50%": { transform: "translate3d(4%, -3%, 0) rotate(8deg)", opacity: "0.8" },
        },
        "aurora-2": {
          "0%, 100%": { transform: "translate3d(0,0,0) scale(1)", opacity: "0.45" },
          "50%": { transform: "translate3d(-5%, 4%, 0) scale(1.12)", opacity: "0.7" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        "gradient-x": {
          "0%, 100%": { backgroundPosition: "0% 50%" },
          "50%": { backgroundPosition: "100% 50%" },
        },
        "glow-pulse": {
          "0%, 100%": { boxShadow: "0 0 0 0 rgba(251,191,36,0.45)" },
          "50%": { boxShadow: "0 0 0 6px rgba(251,191,36,0)" },
        },
        float: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-6px)" },
        },
      },
      animation: {
        ping2: "ping2 1.3s cubic-bezier(0,0,0.2,1) infinite",
        aurora: "aurora 18s ease-in-out infinite",
        "aurora-2": "aurora-2 24s ease-in-out infinite",
        shimmer: "shimmer 1.6s infinite",
        "gradient-x": "gradient-x 6s ease infinite",
        "glow-pulse": "glow-pulse 2s ease-in-out infinite",
        float: "float 6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
