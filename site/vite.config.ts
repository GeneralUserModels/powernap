import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function redirectSlashlessSiteRoutes(): Plugin {
  const routes = new Set(["/tada", "/tada/tabra", "/tada/memex"]);

  return {
    name: "redirect-slashless-site-routes",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url) {
          next();
          return;
        }

        const url = new URL(req.url, "http://localhost");
        if (!routes.has(url.pathname)) {
          next();
          return;
        }

        const target = `${url.pathname}/${url.search}`;
        res.statusCode = 302;
        res.setHeader("Location", target);
        res.end(`Redirecting to ${target}`);
      });
    },
  };
}

export default defineConfig({
  plugins: [redirectSlashlessSiteRoutes(), react()],
  base: "/tada/",
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        tabra: path.resolve(__dirname, "tabra/index.html"),
        memex: path.resolve(__dirname, "memex/index.html"),
      },
    },
  },
});
