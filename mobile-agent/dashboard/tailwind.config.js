/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#06070a",
        panel: "#0e1118",
        accent: "#22d3ee", // cyan — live/active
      },
      keyframes: {
        ping2: {
          "0%": { transform: "scale(1)", opacity: "0.9" },
          "75%, 100%": { transform: "scale(2.4)", opacity: "0" },
        },
      },
      animation: { ping2: "ping2 1.3s cubic-bezier(0,0,0.2,1) infinite" },
    },
  },
  plugins: [],
};
