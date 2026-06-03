---
name: http2-security-hardening
description: "Harden web servers against HTTP/2 vulnerabilities including HTTP/2 Bomb (CVE-pending), Rapid Reset, and other HTTP/2-specific DoS attacks. Covers NGINX, Apache, IIS, Envoy, and Cloudflare Pingora configurations. Use when auditing, configuring, or securing any HTTP/2-enabled web server."
---

# HTTP/2 Security Hardening

## Overview

HTTP/2 introduces powerful features (multiplexing, server push, header compression) that also create new attack surfaces. The most critical current threat is **HTTP/2 Bomb** — a remote denial-of-service affecting default configurations of NGINX, Apache HTTPD, Microsoft IIS, Envoy, and Cloudflare Pingora.

## HTTP/2 Bomb (June 2026)

**What it is:** A remote DoS exploit that abuses HTTP/2 stream multiplexing to overwhelm servers with minimal attacker resources. Discovered by OpenAI Codex through chaining analysis.

**Affected servers (default config):**
- NGINX
- Apache HTTPD
- Microsoft IIS
- Envoy
- Cloudflare Pingora

**Key detail:** The vulnerable behavior exists in each server's **default HTTP/2 configuration** — no special settings required to be vulnerable.

### Immediate Mitigation

#### NGINX
```nginx
# Limit concurrent streams and connection duration
http2_max_concurrent_streams 128;
http2_max_requests 1000;
http2_recv_timeout 30s;
keepalive_timeout 30s;
keepalive_requests 1000;

# Rate limiting
limit_conn_zone $binary_remote_addr zone=addr:10m;
limit_conn addr 50;
```

#### Apache HTTPD
```apache
# In httpd.conf or virtual host
H2MaxStreams 100
H2MaxWorkerIdleSeconds 30
H2StreamMaxMemSize 65536
H2WindowSize 65535

# Enable mod_reqtimeout
RequestReadTimeout header=20-40,MinRate=500 body=20,MinRate=500
```

#### Envoy
```yaml
# In envoy.yaml HTTP connection manager config
http2_protocol_options:
  max_concurrent_streams: 100
  initial_stream_window_size: 65535
  initial_connection_window_size: 1048576
  allow_connect: false
  allow_metadata: false
```

#### IIS
```xml
<!-- In applicationHost.config -->
<system.webServer>
  <httpProtocol>
    <customHeaders>
      <!-- Disable HTTP/2 server push if not needed -->
    </customHeaders>
  </httpProtocol>
  <security>
    <requestFiltering>
      <requestLimits maxAllowedContentLength="30000000" />
    </requestFiltering>
  </security>
</system.webServer>
```

## HTTP/2 Rapid Reset (CVE-2023-44487)

Still relevant in 2026. Same class of attack — stream cancellation abuse.

### Detection
```bash
# Check for rapid stream resets in NGINX logs
awk '$9 ~ /499/ {print $1}' /var/log/nginx/access.log | sort | uniq -c | sort -rn | head -20

# Apache: look for 408 errors from same IPs
grep "408" /var/log/apache2/error.log | awk '{print $NF}' | sort | uniq -c | sort -rn
```

## General HTTP/2 Hardening Checklist

1. **Update everything** — Ensure latest patches for your web server
2. **Limit concurrent streams** — Default is often 100+; reduce to 50-128
3. **Set connection timeouts** — Prevent slow-loris style attacks over HTTP/2
4. **Enable rate limiting** — Per-IP connection and request limits
5. **Disable unused features** — Server push, extended CONNECT if not needed
6. **Monitor HTTP/2 metrics** — Stream resets, RST_STREAM frames, GOAWAY
7. **Use a WAF/CDN** — Cloudflare, AWS Shield, or similar for DDoS absorption
8. **Test with h2load** — `h2load -n1000 -c100 -m100 https://yoursite.com/`

## Verification

```bash
# Check if server advertises HTTP/2
curl -I --http2 https://yoursite.com/ 2>&1 | grep -i "HTTP/2"

# Test with nghttp client
nghttp -nv https://yoursite.com/

# Load test (use carefully)
h2load -n10000 -c100 -m50 https://yoursite.com/
```

## References

- The Hacker News: "New HTTP/2 Bomb Vulnerability Allows Remote DoS on NGINX, Apache, IIS, Envoy & Cloudflare" (June 3, 2026)
- CVE-2023-44487: HTTP/2 Rapid Reset Attack
- Cloudflare blog: "HTTP/2 Rapid Reset: deconstructing the record-breaking DDoS attack"
