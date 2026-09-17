FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-51f83158a70a383f03a4d03dbd8b6ea102cf0361@sha256:253d7ed3409effa7fa59113d93b4b79bb731d8264cdaf4cd60294924d0110a2e
LABEL org.opencontainers.image.title="Meals Planner" \
      org.opencontainers.image.source="https://github.com/lusknchars/meals-planner" \
      org.opencontainers.image.licenses="MIT"
ENV AGENT_ID=meals-planner
COPY --chmod=0644 runtime/persona.md /opt/hermes/plow-seed/persona.md
COPY skills/ /opt/hermes/skills/
# The price rule as a gate rather than as instructions. A bundled `standalone`
# plugin is discovered and then skipped unless its key is on `plugins.enabled`,
# which is what the boot step below writes.
COPY plugin/meals-guard/ /opt/hermes/plugins/meals-guard/
COPY --chmod=0755 image/meals-guard-enable.py /opt/plow/meals-guard-enable.py
COPY LICENSE NOTICE /usr/share/doc/meals-planner/
COPY vendor/ /usr/share/doc/meals-planner/vendor/
COPY vendor/client.pin /opt/plow/agent-index-client.pin
RUN set -eu; \
    sha="$(sed -n 's/^sha=//p' /opt/plow/agent-index-client.pin)"; \
    want="$(sed -n 's/^sha256=//p' /opt/plow/agent-index-client.pin)"; \
    path="$(sed -n 's/^path=//p' /opt/plow/agent-index-client.pin)"; \
    curl -fsS --max-time 60 -o /opt/plow/agent-index-client.py \
      "https://raw.githubusercontent.com/plow-pbc/agent-index-client/${sha}/${path}"; \
    echo "$want  /opt/plow/agent-index-client.py" | sha256sum -c -; \
    chmod 0644 /opt/plow/agent-index-client.py; \
    find /opt/hermes/skills/meals -type d -exec chmod 0755 {} +; \
    find /opt/hermes/skills/meals -type f -exec chmod 0644 {} +; \
    find /opt/hermes/plugins/meals-guard -type d -exec chmod 0755 {} +; \
    find /opt/hermes/plugins/meals-guard -type f -exec chmod 0644 {} +
COPY image/s6-overlay/ /etc/s6-overlay/
RUN chmod 0755 /etc/s6-overlay/s6-rc.d/agent-index/run
