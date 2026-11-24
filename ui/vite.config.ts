import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// En développement, l'explorateur appelle l'API lancée à côté, sur son port habituel.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: { "/api": "http://127.0.0.1:8000" },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Rien n'est placé en ligne dans la page : la politique de sécurité du contenu l'interdit.
    assetsInlineLimit: 0,
  },
});
