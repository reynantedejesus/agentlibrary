# /etc/nginx/conf.d/agentlibrary-proxy.inc
#
# Shared proxy settings, included by every proxying location in
# agentlibrary.conf so the forwarding headers can never drift between them.
#
#   sudo install -m 0644 deploy/agentlibrary-proxy.inc /etc/nginx/conf.d/

proxy_pass http://agentlibrary_app;
proxy_http_version 1.1;
proxy_set_header Connection "";

# The four headers the application depends on. ProxyFix in app/__init__.py
# reads X-Forwarded-For (so audit logs record the real client IP, not
# 127.0.0.1) and X-Forwarded-Proto (so Flask knows the request was HTTPS and
# marks cookies Secure correctly).
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $forwarded_scheme;
proxy_set_header X-Forwarded-Host  $host;
proxy_set_header X-Forwarded-Port  $server_port;

# Strip anything a client tried to inject on these headers before nginx set
# them — a client-supplied X-Real-IP must never reach the audit log.
proxy_set_header X-Forwarded-Server "";

proxy_connect_timeout 10s;
proxy_send_timeout    60s;
proxy_read_timeout    60s;
proxy_buffers 8 16k;
proxy_buffer_size 16k;
proxy_redirect off;
