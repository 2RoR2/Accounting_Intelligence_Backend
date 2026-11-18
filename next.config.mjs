const backendUrl = process.env.BACKEND_URL?.trim().replace(/\/+$/, "");

if (process.env.VERCEL === "1" && !backendUrl) {
  throw new Error(
    "BACKEND_URL must be configured in Vercel environment variables."
  );
}

const config = {
  distDir: process.env.NEXT_BUILD_DIR || ".next",
  outputFileTracingRoot: process.cwd(),
  devIndicators: false,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendUrl || "http://127.0.0.1:8000"}/api/:path*`,
      },
    ];
  },
  webpack(config, { dev }) {
    if (dev) {
      config.cache = false;
    }
    return config;
  },
};
export default config;
