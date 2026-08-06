# NGINX reverse proxy

Media Engine should normally use two public HTTPS hostnames:

- an API hostname for `/v2`, `/admin`, `/docs`, health checks, and temporary legacy endpoints;
- an S3 hostname for short-lived signed artifact downloads.

Do not place MinIO below a rewritten path such as `/s3/`. S3 Signature Version 4 covers the request host, path, and query
string. A dedicated hostname lets NGINX pass all three through unchanged.

The ready-to-adapt examples are:

- [`deploy/nginx/media-engine.conf.example`](../deploy/nginx/media-engine.conf.example) for the public API and S3 hosts;
- [`deploy/nginx/nginx-quic-main.conf.example`](../deploy/nginx/nginx-quic-main.conf.example) for the main-context eBPF
  routing and worker-capacity settings;
- [`deploy/nginx/99-nginx-quic.conf.example`](../deploy/nginx/99-nginx-quic.conf.example) for persistent QUIC socket
  buffer maxima;
- [`deploy/nginx/nginx-quic-gso.service.example`](../deploy/nginx/nginx-quic-gso.service.example) for persistent UDP
  segmentation offload on a verified interface;
- [`deploy/nginx/media-engine-landing.html`](../deploy/nginx/media-engine-landing.html) for the optional static root page;
- [`deploy/nginx/media-engine-upload.conf.example`](../deploy/nginx/media-engine-upload.conf.example) for an optional,
  DNS-only large-upload hostname;
- [`deploy/nginx/media-engine-worker.conf.example`](../deploy/nginx/media-engine-worker.conf.example) for an optional,
  IP-restricted standalone-worker hostname.

## Required environment

Once both public hostnames are reachable, configure the control plane:

```dotenv
MEDIA_ENGINE_S3_PUBLIC_ENDPOINT_URL=https://s3.media.example.com
MEDIA_ENGINE_S3_WORKER_ENDPOINT_URL=https://s3.media.example.com
MEDIA_ENGINE_WORKER_ADVERTISED_API_URL=https://workers.media.example.com
MEDIA_ENGINE_ADMIN_SESSION_COOKIE_SECURE=true
```

`MEDIA_ENGINE_S3_ENDPOINT_URL` remains the internal S3 endpoint used by the control plane.
`MEDIA_ENGINE_S3_PUBLIC_ENDPOINT_URL` constructs client-facing signed artifact URLs, while
`MEDIA_ENGINE_S3_WORKER_ENDPOINT_URL` constructs signed stage download and upload URLs. They may point at the same S3
hostname when it is reachable by both clients and workers.

The public API example returns `404` for `/v2/internal/*`. An all-in-one worker continues to use the private Docker
network. Standalone workers must use a private VPN/LAN route or the restricted worker hostname. Never expose the worker
protocol broadly: a claimed AI stage can contain a temporary provider credential.

The worker hostname remains a normal TLS reverse proxy without an HTTP/3 advertisement. The bundled worker client does
not currently speak QUIC, so opening another public UDP surface would provide no transport benefit there.

## Static root page

The public NGINX example serves `/` directly from a small static HTML file. It does not expose a Media Engine version,
fetch readiness, load JavaScript, request third-party assets, or reach the API process. Root requests are cacheable for
one hour and excluded from the NGINX access log.

Install the example before enabling the vhost:

```bash
sudo install -d -o root -g root -m 0755 /var/www/media-engine
sudo install -o root -g root -m 0644 \
  deploy/nginx/media-engine-landing.html \
  /var/www/media-engine/index.html
```

The page links only to the same-origin API documentation and the public GitHub project. `/docs`, `/admin`, `/v2`, and
all other routes continue to proxy to Media Engine. The S3 hostname must not serve or redirect to this page because S3
signatures depend on exact request behavior.

Serving a static root prevents casual browser traffic or a root-path HTTP flood from consuming application and database
work. It does not replace network-level DDoS protection or rate limiting for the actual API and upload endpoints.

## Certificates, HTTP/2, and HTTP/3

The main example terminates HTTP/3 at NGINX and continues proxying ordinary HTTP/1.1 to Media Engine and MinIO. The
applications do not need native QUIC support. TCP 443 remains open for HTTP/2 fallback; UDP 443 must also be permitted
through the host firewall and any IPv4 NAT/port-forward.

Use NGINX 1.25.0 or newer with `ngx_http_v3_module`. The official NGINX Linux packages include HTTP/3; distribution
packages may use an older build. Confirm the installed binary before changing the listeners:

```bash
nginx -v
nginx -V 2>&1 | grep -- --with-http_v3_module
```

Generate a persistent token key so QUIC validation tokens survive NGINX reloads:

```bash
sudo openssl rand -out /etc/nginx/quic_host.key 32
sudo chown root:root /etc/nginx/quic_host.key
sudo chmod 0600 /etc/nginx/quic_host.key
```

On Linux 5.7 or newer, enable connection-ID-based worker routing in the main context, before `events`. `quic_bpf`
supports QUIC connection migration when `reuseport` creates one socket per worker; it is a routing/reliability feature,
not a promise that one transfer becomes faster:

```nginx
quic_bpf on;

events {
    worker_connections 4096;
}
```

The NGINX master must retain the privileges required to load BPF programs. Validate the exact placement with `nginx -t`;
`quic_bpf` is not valid inside `http` or a normal `conf.d` server snippet.

The API hostname owns the `reuseport` socket and the S3 hostname joins the same UDP port:

```nginx
listen 443 ssl;
listen [::]:443 ssl;
listen 443 quic reuseport rcvbuf=4m sndbuf=4m;
listen [::]:443 quic reuseport rcvbuf=4m sndbuf=4m;
http2 on;

quic_retry on;
quic_host_key /etc/nginx/quic_host.key;
ssl_early_data off;

add_header Alt-Svc 'h3=":443"; ma=86400' always;
```

Specify `reuseport` on exactly one QUIC virtual host for each address and port. Additional name-based virtual hosts use
`listen 443 quic;` and `listen [::]:443 quic;` without repeating `reuseport`. Keep TLS 0-RTT disabled for Media Engine:
early requests can be replayed, which is unsafe for uploads and mutating API calls.

Back the explicit socket sizes with persistent kernel maxima:

```bash
sudo install -o root -g root -m 0644 \
  deploy/nginx/99-nginx-quic.conf.example \
  /etc/sysctl.d/99-nginx-quic.conf
sudo sysctl -p /etc/sysctl.d/99-nginx-quic.conf
```

Linux reports twice the requested `SO_RCVBUF` and `SO_SNDBUF` sizes in `ss -u -m`; a configured 4 MiB buffer therefore
normally appears as 8 MiB. Check `UdpRcvbufErrors`, `UdpSndbufErrors`, and qdisc drops under representative concurrency
instead of increasing buffers without evidence.

Start a rollout with a short `Alt-Svc` lifetime such as `ma=300`. After forced HTTP/3, fallback, upload, and artifact
tests pass, increase it to `ma=86400`. Add `$server_protocol` and `$http3` to an access-log format so real adoption and
request timing can be measured instead of inferred.

NGINX also supports `quic_gso on;`, but enable it only after `ethtool -k <interface>` reports
`tx-udp-segmentation: on` and a multi-packet HTTP/3 response completes without UDP or qdisc errors. It is disabled by
default:

```bash
sudo ethtool -K ens18 tx-udp-segmentation on
ethtool -k ens18 | grep '^tx-udp-segmentation:'
```

When the runtime check succeeds, add `quic_gso on;` in the `http` context. Install and edit
[`nginx-quic-gso.service.example`](../deploy/nginx/nginx-quic-gso.service.example) to make the interface setting survive
reboots:

```bash
sudo install -o root -g root -m 0644 \
  deploy/nginx/nginx-quic-gso.service.example \
  /etc/systemd/system/nginx-quic-gso.service
sudo systemctl daemon-reload
sudo systemctl enable --now nginx-quic-gso.service
```

For virtual machines, expose at least as many VirtIO combined queues as the NGINX worker count, up to the useful vCPU
count. Verify the effective guest state with `ethtool -l <interface>` and `networkctl status <interface>`; a hypervisor
configuration change is not complete until the guest reports the new current queue count.

Use a full certificate chain and its matching private key. Cloudflare Origin CA certificates are appropriate only while
the hostname remains proxied through Cloudflare. A DNS-only hostname needs a publicly trusted certificate, such as one
issued by Let's Encrypt.

Once both public DNS records point directly to the reverse proxy, issue one ECDSA certificate containing both names:

```bash
sudo certbot certonly --nginx \
  --key-type ecdsa \
  --cert-name media.example.com \
  --email admin@example.com \
  --agree-tos \
  -d media.example.com \
  -d s3.media.example.com
```

Both public server blocks can then use:

```nginx
ssl_certificate     /etc/letsencrypt/live/media.example.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/media.example.com/privkey.pem;
```

Verify the installed renewal timer and perform a staging renewal before considering the deployment complete:

```bash
systemctl status snap.certbot.renew.timer
sudo certbot renew --cert-name media.example.com --dry-run
```

## Large uploads

NGINX defaults `client_max_body_size` to 1 MB. The public example raises it to 10 GB and disables request buffering so a
video streams to Media Engine instead of first filling the reverse proxy's temporary storage. Set
`MEDIA_ENGINE_MAX_UPLOAD_BYTES` when the application should enforce a matching hard limit.

Cloudflare applies a separate request-body ceiling before traffic reaches NGINX. At the time of writing, its documented
limits are 100 MB on Free and Pro, 200 MB on Business, and 500 MB by default on Enterprise:

<https://developers.cloudflare.com/support/troubleshooting/http-status-codes/4xx-client-error/error-413/>

Existing integrations may avoid that ceiling because they use a private address, upload smaller media, or resolve the
hostname without the Cloudflare proxy. Verify the real route with a media file larger than the account limit. If large
public uploads must work, use one of these approaches:

1. send trusted same-network products directly to the private API address;
2. use the DNS-only [`media-engine-upload.conf.example`](../deploy/nginx/media-engine-upload.conf.example) with a
   publicly trusted certificate while keeping the main API behind Cloudflare;
3. add a future presigned multipart-upload flow so media goes directly to S3 in bounded parts.

Option 3 is the best generic long-term design because it avoids routing large bodies through the control plane.

## Direct DNS and local routing

For direct traffic, publish an IPv4 `A` record and, when the origin is reachable over IPv6, an `AAAA` record for every
public hostname. Do not publish an address family that cannot reach ports 80 and 443 on the reverse proxy because ACME
validation and clients may prefer it over the working address.

Networks that run Media Engine products locally can use split-horizon DNS:

- public DNS resolves the hostnames to the reverse proxy's public IPv4/IPv6 addresses;
- local DNS resolves the same hostnames to the reverse proxy's private address.

The HTTPS hostname and certificate remain identical, while local uploads and signed-artifact downloads stay on the LAN.
This is preferable to giving products a separate private URL because `MEDIA_ENGINE_S3_PUBLIC_ENDPOINT_URL` and
`MEDIA_ENGINE_S3_WORKER_ENDPOINT_URL` can retain one certificate hostname while split DNS keeps local transfers on the
LAN.

## Cloudflare

When both hostnames are proxied:

1. use **Full (strict)** SSL/TLS mode;
2. ensure the Origin CA certificate covers the API hostname and S3 hostname;
3. create a Cache Rule that bypasses caching for the entire S3 hostname;
4. keep the NGINX `Cache-Control: private, no-store` response header as defense in depth.

Do not proxy the MinIO console port. Manage MinIO only from a trusted network.

## Validate

Check the complete NGINX configuration before reloading:

```bash
nginx -t
systemctl reload nginx
```

Test the origin locally before depending on public DNS:

```bash
curl -k --resolve media.example.com:443:127.0.0.1 \
  https://media.example.com/readyz

curl -k --resolve s3.media.example.com:443:127.0.0.1 \
  https://s3.media.example.com/minio/health/live
```

Then validate the public route:

```bash
curl https://media.example.com/readyz
curl https://s3.media.example.com/minio/health/live
```

Use a curl build whose feature list contains `HTTP3` to prove UDP/QUIC independently of `Alt-Svc`:

```bash
curl -V
curl --http3-only https://media.example.com/readyz
curl --http3-only https://s3.media.example.com/minio/health/live
curl --http2 https://media.example.com/readyz
```

The HTTP/3 requests must report protocol version 3 in the client or `HTTP/3.0` in the NGINX protocol log. The forced
HTTP/2 request proves that clients still work when UDP is unavailable. Also upload a representative large video with
`--http3-only` and range-read a signed S3 artifact; small health requests do not exercise long-lived NAT state or
streaming behaviour.

Finally, request an artifact through `/v2/jobs/{job_id}/artifacts` and confirm that its signed URL:

- starts with the configured S3 HTTPS hostname;
- downloads successfully;
- stops working after `MEDIA_ENGINE_ARTIFACT_URL_EXPIRY_SECONDS`;
- is not served from a CDN cache after expiry.
