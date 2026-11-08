const API = process.env.API_INTERNAL_URL ?? "http://localhost:8001";

/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,
  // Same-origin proxy so session cookies are first-party and the API stays private.
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${API}/api/v1/:path*` },
      // OpenAI-compatible inference API for OpenCode, Cline, Goose, Aider, ...
      { source: "/v1/:path*", destination: `${API}/v1/:path*` },
    ];
  },
};
