{{/*
egress-gw â€” reusable egress gateway with guardrails sidecar.

Renders ServiceAccount, ConfigMap (Envoy config), Deployment, and Service
in a single namespace. Each cell that needs an egress gateway includes this
helper with its own context.

Expected argument: a dict with keys
  root      â€” the chart root context (.) â€” needed for .Files, .Chart, .Release, .Values.global
  namespace â€” target namespace (string)
  values    â€” the egress values block for this cell, e.g. .Values.customerServiceAssistantAgentCell.egress
              Required keys: replicas, envoyImage, envsubstImage, guardrailsImage
              Optional keys (set per provider in use):
                openaiApiKeySecretName, anthropicApiKeySecretName, geminiApiKeySecretName
              Omitted providers will not have a secret volume mounted; requests
              routed to them will 503 from the Lua key-injection filter.

Usage:
  {{- include "cell-based-agent.egress-gw" (dict
       "root" .
       "namespace" .Values.customerServiceAssistantAgentCell.namespace
       "values" .Values.customerServiceAssistantAgentCell.egress) }}
*/}}
{{- define "cell-based-agent.egress-gw" -}}
{{- $root := .root -}}
{{- $ns := .namespace -}}
{{- $v := .values -}}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: egress-gw
  namespace: {{ $ns }}
  labels:
    {{- include "cell-based-agent.labels" $root | nindent 4 }}
    app.kubernetes.io/name: egress-gw
    app.kubernetes.io/component: gateway
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: egress-gw-envoy-config
  namespace: {{ $ns }}
  labels:
    {{- include "cell-based-agent.labels" $root | nindent 4 }}
    app.kubernetes.io/name: egress-gw
    app.kubernetes.io/component: gateway
data:
  envoy.yaml: |
{{ $root.Files.Get "files/common/egress-envoy.yaml" | indent 4 }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: egress-gw
  namespace: {{ $ns }}
  labels:
    {{- include "cell-based-agent.labels" $root | nindent 4 }}
    app.kubernetes.io/name: egress-gw
    app.kubernetes.io/component: gateway
spec:
  replicas: {{ $v.replicas }}
  selector:
    matchLabels:
      app.kubernetes.io/name: egress-gw
  template:
    metadata:
      labels:
        app.kubernetes.io/name: egress-gw
        app.kubernetes.io/component: gateway
    spec:
      serviceAccountName: egress-gw
      securityContext:
        {{- include "cell-based-agent.podSecurityContext" $root | nindent 8 }}
      initContainers:
        - name: envsubst
          image: {{ $v.envsubstImage }}
          command: ["sh", "-c", "envsubst < /config-template/envoy.yaml > /etc/envoy/envoy.yaml"]
          env:
            - name: OTEL_COLLECTOR_HOST
              value: {{ $root.Values.global.otelCollectorHost | quote }}
          volumeMounts:
            - name: envoy-config-template
              mountPath: /config-template
            - name: envoy-config-rendered
              mountPath: /etc/envoy
          securityContext:
            runAsUser: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
      containers:
        - name: guardrails
          image: {{ $v.guardrailsImage }}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 50052
              name: grpc
              protocol: TCP
          env:
            - name: OTEL_COLLECTOR_HOST
              value: {{ $root.Values.global.otelCollectorHost | quote }}
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            runAsUser: 1000
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          readinessProbe:
            grpc:
              port: 50052
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            grpc:
              port: 50052
            initialDelaySeconds: 10
            periodSeconds: 30
        - name: envoy
          image: {{ $v.envoyImage }}
          args: ["-c", "/etc/envoy/envoy.yaml", "--service-cluster", "egress-gw"]
          ports:
            - containerPort: 8080
              name: http
          volumeMounts:
            - name: spire-agent-socket
              mountPath: /run/spire/sockets
              readOnly: true
            - name: envoy-config-rendered
              mountPath: /etc/envoy
              readOnly: true
            {{- if $v.openaiApiKeySecretName }}
            - name: openai-api-key
              mountPath: /run/secrets/openai
              readOnly: true
            {{- end }}
            {{- if $v.anthropicApiKeySecretName }}
            - name: anthropic-api-key
              mountPath: /run/secrets/anthropic
              readOnly: true
            {{- end }}
            {{- if $v.geminiApiKeySecretName }}
            - name: gemini-api-key
              mountPath: /run/secrets/gemini
              readOnly: true
            {{- end }}
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 256Mi
          securityContext:
            runAsUser: 101
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 30
      volumes:
        - name: spire-agent-socket
          csi:
            driver: csi.spiffe.io
            readOnly: true
        - name: envoy-config-template
          configMap:
            name: egress-gw-envoy-config
        - name: envoy-config-rendered
          emptyDir: {}
        {{- if $v.openaiApiKeySecretName }}
        - name: openai-api-key
          secret:
            secretName: {{ $v.openaiApiKeySecretName }}
            optional: true
            items:
              - key: api-key
                path: openai-api-key
        {{- end }}
        {{- if $v.anthropicApiKeySecretName }}
        - name: anthropic-api-key
          secret:
            secretName: {{ $v.anthropicApiKeySecretName }}
            optional: true
            items:
              - key: api-key
                path: anthropic-api-key
        {{- end }}
        {{- if $v.geminiApiKeySecretName }}
        - name: gemini-api-key
          secret:
            secretName: {{ $v.geminiApiKeySecretName }}
            optional: true
            items:
              - key: api-key
                path: gemini-api-key
        {{- end }}
---
apiVersion: v1
kind: Service
metadata:
  name: egress-gw
  namespace: {{ $ns }}
  labels:
    {{- include "cell-based-agent.labels" $root | nindent 4 }}
    app.kubernetes.io/name: egress-gw
    app.kubernetes.io/component: gateway
spec:
  selector:
    app.kubernetes.io/name: egress-gw
  ports:
    - port: 8080
      targetPort: 8080
      name: http
{{- end -}}
