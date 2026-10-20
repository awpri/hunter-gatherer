/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // Tier colors used for the route badges.
        tier: { local: "#22d3ee", cheap: "#fbbf24", cloud: "#f472b6" },
      },
    },
  },
  plugins: [],
};
