{{/*
Common labels applied to every Kubernetes object created by this chart.
Selectors deliberately exclude these (selectors are immutable).
*/}}
{{- define "cell-based-agent.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: multi-cell-based-agent
{{- end -}}

{{/*
Pod-level securityContext for non-root workloads.
*/}}
{{- define "cell-based-agent.podSecurityContext" -}}
runAsNonRoot: true
seccompProfile:
  type: RuntimeDefault
{{- end -}}
