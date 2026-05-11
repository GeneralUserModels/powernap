import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

function redirectSiteRoutes(): Plugin {
  const slashlessRoutes = new Set(["/tada", "/tada/tabracadabra", "/tada/memex"]);
  const retiredRoutes = new Set(["/tada/tabra", "/tada/tabra/"]);

  return {
    name: "redirect-site-routes",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url) {
          next();
          return;
        }

        const url = new URL(req.url, "http://localhost");
        if (retiredRoutes.has(url.pathname)) {
          res.statusCode = 404;
          res.end("Not found");
          return;
        }

        if (!slashlessRoutes.has(url.pathname)) {
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
  plugins: [redirectSiteRoutes(), react()],
  base: "/tada/",
  build: {
    rollupOptions: {
      input: {
        main: path.resolve(__dirname, "index.html"),
        tabracadabra: path.resolve(__dirname, "tabracadabra/index.html"),
        memex: path.resolve(__dirname, "memex/index.html"),
      },
    },
  },
});
