# WebSocket settings shared by every config path.
#
# Buffering is off: a proxy must not hold bytes waiting for a full buffer, and
# compression is off because it costs CPU and adds latency for an already
# encrypted tunnel.
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection $conn_upgrade;
proxy_set_header Host $host;
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto https;

proxy_buffering off;
proxy_request_buffering off;
proxy_socket_keepalive on;
proxy_read_timeout 300s;
proxy_send_timeout 300s;