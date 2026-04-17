# Xray teardown — strip every relay-specific bit so the config looks
# like a direct-egress node again. Used by the disable branch when a
# relay has no active links (last exit detached).
#
# No args — purely destructive (takes config.json, returns config.json
# without direct-wg* outbounds/rules and without sockopt on default
# direct outbound).

.outbounds = (
  (.outbounds // [])
  | map(select(((.tag // "") | startswith("direct-wg")) | not))
  | map(
      if (.tag == "direct" and .protocol == "freedom")
      then del(.streamSettings)
      else .
      end
    )
)
| .routing.rules = (
    (.routing.rules // [])
    | map(select(((.outboundTag // "") | startswith("direct-wg")) | not))
  )
