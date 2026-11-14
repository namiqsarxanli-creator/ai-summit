import https from "node:https";
import http from "node:http";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const root = process.cwd();
const ip = process.env.FACEMARK_PHONE_IP;
const port = Number(process.env.FACEMARK_PHONE_PORT || 3443);
if (!ip || !/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) {
  throw new Error("FACEMARK_PHONE_IP must be a LAN IPv4 address");
}
const certDir = join(root, ".env.phone");
const expectedHost = `${ip}:${port}`;
const server = https.createServer({
  key: readFileSync(join(certDir, "server.key")),
  cert: readFileSync(join(certDir, "server.crt")),
}, (request, response) => {
  if (request.headers.host !== expectedHost) {
    response.writeHead(421, { "content-type": "text/plain; charset=utf-8" });
    response.end("Unknown FACEMARK host");
    return;
  }
  const headers = {
    ...request.headers,
    host: "localhost:3000",
    "x-forwarded-host": expectedHost,
    "x-forwarded-proto": "https",
  };
  delete headers.connection;
  const upstream = http.request({
    hostname: "127.0.0.1",
    port: 3000,
    path: request.url,
    method: request.method,
    headers,
  }, (upstreamResponse) => {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    upstreamResponse.pipe(response);
  });
  upstream.on("error", () => {
    if (!response.headersSent) response.writeHead(502);
    response.end("FACEMARK frontend unavailable");
  });
  request.pipe(upstream);
});
server.listen(port, "0.0.0.0", () => {
  process.stdout.write(`FACEMARK iPhone HTTPS: https://${expectedHost}/\n`);
});