# The DAST merge gate. Reads ZAP's JSON report and prints one tab-separated
# line per finding that blocks: Medium or High risk, from a rule that
# accepted-alerts.tsv does not accept. No output means the gate passes.
#
#   jq -r --rawfile accepted deploy/dast/accepted-alerts.tsv \
#     -f deploy/dast/gate.jq zap-reports/zap-report.json
#
# Gating on risk rather than listing rules to FAIL is deliberate: a rule ZAP
# adds in a later release blocks by default instead of being silently WARN.

def accepted_rules:
  $accepted
  | split("\n")
  | map(sub("\r$"; "") | select(length > 0 and (startswith("#") | not)) | split("\t"))
  | if any(.[]; length < 2 or (.[1] | test("^\\s*$")))
    then error("accepted-alerts.tsv: every line needs a rule id, a tab, and the reason it is accepted")
    else map(.[0])
    end;

accepted_rules as $ids
| .site[]?.alerts[]?
| select((.riskcode | tonumber) >= 2)
| select(.pluginid | IN($ids[]) | not)
| [.riskdesc, .pluginid, .name, (.instances | length | tostring), (.instances[0].uri // "")]
| @tsv
