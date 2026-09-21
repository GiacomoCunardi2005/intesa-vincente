#!/usr/bin/env bash
# Copia il contesto Docker sull'OptiPlex e costruisce/avvia l'app sul posto.
set -euo pipefail

usage() {
  printf 'Uso: %s [utente@]IP\n' "$(basename "$0")"
  printf 'Esempio: %s jack@192.168.1.10\n' "$(basename "$0")"
  printf 'Opzionale: SSH_PORT=2222 %s jack@192.168.1.10\n' "$(basename "$0")"
}

case "${1:-}" in
  -h|--help)
    usage
    exit 0
    ;;
esac

target="${1:-}"
if [[ -z "$target" ]]; then
  read -r -p 'IP o utente@IP dell’OptiPlex: ' target
fi
if [[ -z "$target" || "$target" == *[[:space:]]* ]]; then
  printf 'Inserisci un IP valido, eventualmente preceduto da utente@.\n' >&2
  exit 1
fi

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
build_files=(.dockerignore Dockerfile compose.yaml requirements.txt server.py web res)
for file in "${build_files[@]}"; do
  if [[ ! -e "$project_dir/$file" ]]; then
    printf 'File necessario mancante: %s\n' "$file" >&2
    exit 1
  fi
done

ssh_options=()
if [[ -n "${SSH_PORT:-}" ]]; then
  ssh_options=(-p "$SSH_PORT")
fi

printf 'Invio dei file e build Docker su %s…\n' "$target"
tar -C "$project_dir" -czf - "${build_files[@]}" |
  ssh "${ssh_options[@]}" -- "$target" '
    set -e
    command -v docker >/dev/null
    docker compose version >/dev/null
    mkdir -p "$HOME/intesa-vincente"
    tar -xzf - -C "$HOME/intesa-vincente"
    cd "$HOME/intesa-vincente"
    docker compose --project-name intesa-vincente up -d --build
    docker compose --project-name intesa-vincente ps
  '

printf 'Deploy completato: il server ascolta sulla porta 5522.\n'
