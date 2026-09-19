FROM public.ecr.aws/e1h7x4a2/plow-cloud-agents:base-ef0019372ff8bca593611b31ebd2e08f9f1458ff@sha256:a8a2f97ad78b8192d80a984dce81d3bf5a9a883d18cb7b677704913a09b56aee
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
RUN set -eu; \
    find /opt/hermes/skills/meals -type d -exec chmod 0755 {} +; \
    find /opt/hermes/skills/meals -type f -exec chmod 0644 {} +; \
    find /opt/hermes/plugins/meals-guard -type d -exec chmod 0755 {} +; \
    find /opt/hermes/plugins/meals-guard -type f -exec chmod 0644 {} +
COPY image/s6-overlay/ /etc/s6-overlay/
