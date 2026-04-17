# Xray config reconciliation for multi-link relays (G.6).
#
# Args (passed via --argjson/--arg from the shell task):
#   $fan_out  : [ {interface: "wgN", emails: [...]}, ... ]
#   $primary  : "wgN" — interface used by the default "direct" outbound's
#               sockopt; empty string means strip sockopt.
#
# Stored as a plain file instead of inline shell because Ansible 2.17's
# argument splitter chokes on jq's curly-brace-heavy DSL mixed with
# Jinja {{ }} expressions inside a shell block scalar. Keeping the jq
# program in a separate file dodges the parser interaction entirely.

# --- outbounds -----------------------------------------------------
# Drop every stale direct-wg* outbound; keep direct/block/api/etc.
.outbounds = (
  (.outbounds // [])
  | map(select(((.tag // "") | startswith("direct-wg")) | not))
)
# Append a fresh direct-wgN freedom outbound per wanted link.
| .outbounds += (
    $fan_out | map({
      protocol: "freedom",
      tag: ("direct-" + .interface),
      streamSettings: {
        sockopt: {interface: .interface, tcpKeepAliveInterval: 30}
      }
    })
  )
# Patch the default direct outbound sockopt (or strip it).
| .outbounds = (.outbounds | map(
    if (.tag == "direct" and .protocol == "freedom") then
      if $primary != "" then
        .streamSettings = {
          sockopt: {interface: $primary, tcpKeepAliveInterval: 30}
        }
      else
        del(.streamSettings)
      end
    else . end
  ))
# --- routing rules -------------------------------------------------
# Drop every stale direct-wg* rule; keep api-in etc.
| .routing.rules = (
    ((.routing.rules // [])
     | map(select(((.outboundTag // "") | startswith("direct-wg")) | not)))
    + (
      $fan_out
      | map({
          type: "field",
          user: .emails,
          outboundTag: ("direct-" + .interface)
        })
      | map(select((.user | length) > 0))
    )
  )
