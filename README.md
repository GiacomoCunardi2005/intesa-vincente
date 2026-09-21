# L'intesa vincente — server web Docker

Repository della sola versione multiplayer web: server Python, interfaccia,
liste di parole, immagini, audio e font. Il vecchio gioco GTK/Python e il JAR
Java restano locali ma non sono inclusi nel repository né nel container.

La partita attiva vive in memoria: avvia una sola istanza del container.
Riavviarla azzera la partita attiva, ma la classifica resta nel volume Docker
`intesa-records` (non usare `docker compose down -v` se vuoi conservarla).

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

## Deploy via SSH

Con Docker e Docker Compose già installati sull'OptiPlex, lo script trasferisce
solo il contesto necessario alla build, lo salva in `~/intesa-vincente` sul
server e avvia l'aggiornamento senza cancellare il volume dei record:

```bash
./deploy-ssh.sh jack@192.168.1.10
```

Puoi anche eseguirlo senza argomenti: chiederà IP o `utente@IP`. Per una porta
SSH non standard usa, ad esempio, `SSH_PORT=2222 ./deploy-ssh.sh jack@192.168.1.10`.
Un eventuale file remoto `.env` non viene sovrascritto.

In alternativa, senza Compose:

```bash
docker build -t intesa-vincente .
docker run -d --name intesa-vincente --restart unless-stopped \
  -p 5522:5522 \
  -v intesa-records:/data \
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
