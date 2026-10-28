{{/* Player passwords belong in the credentials Secret, never a ConfigMap. */}}
{{- define "dune.validateCredentials" -}}
{{- if .Values.world.serverLoginPassword -}}
{{- fail "Move world.serverLoginPassword to the server-login-password key in credentials.existingSecret; see README.md" -}}
{{- end -}}
{{- end -}}

{{/* Preserve each map's upstream memory limit when overriding CPU or requests. */}}
{{- define "dune.mapResources" -}}
{{- $defaults := dict "limits" (dict "memory" .memory) -}}
{{- $overrides := index .root.Values.world.mapResources .map | default dict -}}
{{- toYaml (mergeOverwrite $defaults (deepCopy $overrides)) -}}
{{- end -}}
