import { defineConfig } from 'vite'

// the app's own screens (setup, folders, indexing, settings); the search page itself is served by the local engine
export default defineConfig({
  clearScreen: false,
  server: { port: 1420, strictPort: true, host: '127.0.0.1' },
  build: { outDir: 'dist', target: 'es2021', emptyOutDir: true },
})
