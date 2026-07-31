/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export: the whole UI becomes plain files in `out/`, which are synced
  // to S3 and served by CloudFront. No Node server to run, host, or pay for —
  // CloudFront also proxies /api/* to the backend, so the app is same-origin
  // and the session cookie works without any CORS configuration.
  output: "export",
  trailingSlash: true,
  images: { unoptimized: true },
  reactStrictMode: true,
};

export default nextConfig;
