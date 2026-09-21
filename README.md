# L'intesa vincente — server web Docker

Repository della sola versione multiplayer web: server Python, interfaccia,
liste di parole, immagini, audio e font. Il vecchio gioco GTK/Python e il JAR
Java restano locali ma non sono inclusi nel repository né nel container.

La partita vive in memoria: avvia una sola istanza del container. Riavviarla
azzera la partita attiva.

## Avvio sull'OptiPlex, porta 5522

```bash
docker compose up -d --build
docker compose ps
curl http://127.0.0.1:5522/health
```

Il container espone `5522` e Compose pubblica `5522:5522` sull'OptiPlex.
Per fermarlo:

```bash
docker compose down
```

In alternativa, senza Compose:

```bash
docker build -t intesa-vincente .
docker run -d --name intesa-vincente --restart unless-stopped \
  -p 5522:5522 \
  -e ALLOWED_ORIGINS=https://intesa.cunardi.com \
  intesa-vincente
```

## Dominio

Il file `compose.yaml` accetta di default richieste WebSocket provenienti da
`https://intesa.cunardi.com`. Con Caddy sull'OptiPlex:

```caddy
intesa.cunardi.com {
    reverse_proxy 127.0.0.1:5522
}
```

Per una prova diretta tramite IP/porta, imposta prima l'origine del browser,
ad esempio:

```bash
ALLOWED_ORIGINS=http://192.168.1.10:5522 docker compose up -d --build
```

Sostituisci l'indirizzo con quello reale dell'OptiPlex. Caddy inoltra già anche
le connessioni WebSocket.

## Verifica locale senza Docker

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
HOST=0.0.0.0 PORT=5522 python3 server.py
python3 server.py --self-test
```
