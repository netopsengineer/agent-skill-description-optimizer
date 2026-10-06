# Single scanner version owner; Dependabot's docker ecosystem maintains this pin.
FROM ghcr.io/google/osv-scanner:v2.6.0@sha256:afd838850ac1a0fcc15ff4a041dc9ba11123c3f0d2666217a5f0fcf9222b55fa

# Upstream keeps its entrypoint under /root. Relocate the unchanged binary so the
# scanner can run as the checkout owner's unprivileged UID with a read-only root.
RUN cp /root/osv-scanner /usr/local/bin/osv-scanner && chmod 0755 /usr/local/bin/osv-scanner
ENTRYPOINT ["/usr/local/bin/osv-scanner"]
