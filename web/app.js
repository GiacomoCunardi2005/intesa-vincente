const $ = (selector) => document.querySelector(selector);

const joinPanel = $("#join-panel");
const joinForm = $("#join-form");
const nameInput = $("#name");
const roleInputs = [...document.querySelectorAll('input[name="initial-role"]')];
const game = $("#game");
const connection = $("#connection");
const word = $("#word");
const timer = $("#timer");
const score = $("#score");
const status = $("#status");
const timeAlert = $("#time-alert");
const wordFrame = $("#word-frame");
const roleBadge = $("#role-badge");
const roleHint = $("#role-hint");
const notice = $("#notice");
const joinNotice = $("#join-notice");
const controlsPanel = $("#controls-panel");
const stopPanel = $("#stop-panel");
const playerList = $("#player-list");
const spectateButton = $("#spectate-button");
const claimSeatActions = $("#claim-seat-actions");
const spectators = $("#spectators");
const recordsList = $("#records-list");
const helpButton = $("#help-button");
const rulesDialog = $("#rules-dialog");
const ruleImage = $("#rule-image");
const rulePage = $("#rule-page");
const previousRule = $("#previous-rule");
const nextRule = $("#next-rule");
const statRound = $("#stat-round");
const statScore = $("#stat-score");
const statCorrect = $("#stat-correct");
const statWrong = $("#stat-wrong");
const statPasses = $("#stat-passes");
const statDoubles = $("#stat-doubles");
const actionButtons = [...document.querySelectorAll("[data-action]")];

const TOKEN_KEY = "intesa-vincente-token";
const NAME_KEY = "intesa-vincente-name";
const ROLE_KEY = "intesa-vincente-initial-role";
const frames = { normal: "normale.png", correct: "giusto.png", wrong: "errore.png" };
const sounds = {
  start: "gong.wav",
  correct: "giusto.wav",
  wrong: "errore.wav",
  pass: "raddoppio-passo.wav",
  double: "raddoppio-passo.wav",
  next: "cambioParola.wav",
  click: "click.wav",
};
const rules = ["info1.jpg", "info2.jpg", "info3.jpg"];
const roleLabels = {
  controller: "Suggeritore con comandi",
  helper: "Secondo suggeritore",
  guesser: "Indovino",
  spectator: "Spettatore",
};
const initialRoleForSeat = { 1: "controller", 2: "helper", 3: "guesser" };
let socket;
let roomState;
let joinedName = "";
let joinedInitialRole = "controller";
let reconnectDelay = 500;
let reconnectTimer;
let currentRule = 0;
let pendingSeat;
let actionCooldownUntil = 0;
let actionCooldownTimer;
let soundsUnlocked = false;

nameInput.value = localStorage.getItem(NAME_KEY) || "";
const savedRole = localStorage.getItem(ROLE_KEY);
const savedRoleInput = roleInputs.find((input) => input.value === savedRole);
if (savedRoleInput) savedRoleInput.checked = true;

function selectedInitialRole() {
  return roleInputs.find((input) => input.checked)?.value || "controller";
}

function rememberInitialRole(role) {
  joinedInitialRole = role;
  localStorage.setItem(ROLE_KEY, role);
}

function setConnection(message) {
  connection.textContent = message;
}

function setNotice(message = "") {
  notice.textContent = message;
  joinNotice.textContent = message;
}

function unlockSounds() {
  if (soundsUnlocked) return;
  soundsUnlocked = true;
  new Audio("/assets/sounds/null.wav").play().catch(() => {});
}

function playSound(name) {
  if (!soundsUnlocked) return;
  new Audio(`/assets/sounds/${sounds[name]}`).play().catch(() => {});
}

function playRoomSound(previous, room) {
  if (!previous) return;
  if (room.feedback === "correct" && previous.feedback !== "correct") playSound("correct");
  else if (room.feedback === "wrong" && previous.feedback !== "wrong") playSound("wrong");
  else if (room.doubles > previous.doubles) playSound("double");
  else if (room.passes > previous.passes) playSound("pass");
  else if (room.phase === "running" && previous.phase === "feedback") playSound("next");
  else if (room.phase === "running" && previous.phase !== "running") playSound("start");
}

function socketUrl() {
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}/ws`;
}

function sendJoin() {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({
      type: "join",
      name: joinedName,
      initialRole: joinedInitialRole,
      token: localStorage.getItem(TOKEN_KEY),
    }));
  }
}

function connect(name, initialRole) {
  clearTimeout(reconnectTimer);
  joinedName = name;
  joinedInitialRole = initialRole;
  setConnection("Connessione in corso…");
  socket = new WebSocket(socketUrl());

  socket.addEventListener("open", () => {
    reconnectDelay = 500;
    sendJoin();
  });

  socket.addEventListener("message", ({ data }) => {
    let payload;
    try {
      payload = JSON.parse(data);
    } catch {
      return;
    }
    if (payload.type === "joined") {
      localStorage.setItem(TOKEN_KEY, payload.token);
      localStorage.setItem(NAME_KEY, joinedName);
      localStorage.setItem(ROLE_KEY, joinedInitialRole);
      joinPanel.hidden = true;
      game.hidden = false;
      setNotice();
      setConnection("Connesso");
    } else if (payload.type === "state") {
      playRoomSound(roomState?.room, payload.room);
      roomState = payload;
      render();
    } else if (payload.type === "error") {
      pendingSeat = undefined;
      setNotice(payload.message || "Operazione non disponibile.");
    }
  });

  socket.addEventListener("close", () => {
    setConnection("Disconnesso: provo a rientrare entro 30 s…");
    if (joinedName) {
      reconnectTimer = setTimeout(() => {
        reconnectDelay = Math.min(reconnectDelay * 2, 5000);
        connect(joinedName, joinedInitialRole);
      }, reconnectDelay);
    }
  });

  socket.addEventListener("error", () => setConnection("Connessione non disponibile"));
}

function canUse(action, room, you) {
  const canControl = you.can_control === true;
  if (action === "space") {
    return room.phase === "running"
      ? you.role === "guesser"
      : canControl && ["idle", "double-ready"].includes(room.phase);
  }
  if (!canControl) return false;
  if (action === "finish") return room.started && room.phase !== "finished";
  if (["correct", "wrong"].includes(action)) return ["stopped", "round-ended"].includes(room.phase);
  if (action === "next-round") return room.phase === "round-ready";
  if (action === "pass") return ["running", "stopped"].includes(room.phase);
  if (action === "double") return room.phase === "idle" && room.score >= 2 && room.doubles < 2;
  return true;
}

function describeRole(role) {
  if (role === "controller") return "Vedi la parola e gestisci tutti i comandi della squadra.";
  if (role === "helper") return "Vedi la parola e dai gli indizi alternandoti al suggeritore con comandi.";
  if (role === "guesser") return "Non ricevi la parola: ascolta gli indizi e premi Spazio per fermare il tempo.";
  return "Segui la partita e le statistiche della squadra in tempo reale.";
}

function roleForSeat(room, seat) {
  return ["controller", "helper", "guesser"].find((role) => room[`${role}_seat`] === seat);
}

function renderMembership(room, you) {
  const isPlayer = Number.isInteger(you.seat);
  spectateButton.hidden = !isPlayer;
  claimSeatActions.hidden = isPlayer;
  claimSeatActions.replaceChildren();
  if (isPlayer) {
    if (pendingSeat === you.seat) {
      rememberInitialRole(initialRoleForSeat[you.seat]);
      pendingSeat = undefined;
    }
    return;
  }

  rememberInitialRole("spectator");
  const freeSeats = room.players
    .map((player, index) => (!player ? index + 1 : null))
    .filter(Boolean);
  const label = document.createElement("p");
  label.textContent = freeSeats.length ? "Posti disponibili" : "Nessun posto libero.";
  claimSeatActions.append(label);
  freeSeats.forEach((seat) => {
    const role = roleForSeat(room, seat);
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = `Entra come ${roleLabels[role]} (posto ${seat})`;
    button.disabled = socket?.readyState !== WebSocket.OPEN;
    button.addEventListener("click", () => sendMembership("claim-seat", seat));
    claimSeatActions.append(button);
  });
}

function renderRecords(records) {
  const rows = Array.isArray(records) ? records : [];
  if (!rows.length) {
    const empty = document.createElement("li");
    empty.className = "record-empty";
    empty.textContent = "Nessun record ancora.";
    recordsList.replaceChildren(empty);
    return;
  }
  recordsList.replaceChildren(...rows.map((record, index) => {
    const item = document.createElement("li");
    const rank = document.createElement("span");
    const team = document.createElement("strong");
    const correct = document.createElement("span");
    rank.className = "record-rank";
    team.className = "record-team";
    correct.className = "record-score";
    rank.textContent = index + 1;
    team.textContent = typeof record?.team === "string" ? record.team : "Squadra";
    correct.textContent = `${Number.isInteger(record?.correct) ? record.correct : 0} giuste`;
    item.append(rank, team, correct);
    return item;
  }));
}

function render() {
  const { you, room } = roomState;
  const canControl = you.can_control === true;
  const wordIsHidden = room.word === null;
  word.textContent = room.word ?? "PAROLA NASCOSTA";
  word.classList.toggle("is-hidden", wordIsHidden);
  word.setAttribute("aria-label", wordIsHidden ? "Parola nascosta" : `Parola: ${room.word}`);
  timer.textContent = room.remaining;
  score.textContent = room.score;
  status.textContent = room.status;
  timeAlert.textContent = room.phase === "running" ? "VIA AL TEMPO" : ["stopped", "round-ended"].includes(room.phase) ? "STOP AL TEMPO" : "";
  timeAlert.hidden = !timeAlert.textContent;
  wordFrame.src = `/assets/img/${frames[room.feedback] || frames.normal}`;
  roleBadge.textContent = roleLabels[you.role] || "Spettatore";
  roleHint.textContent = describeRole(you.role);
  controlsPanel.hidden = !canControl;
  stopPanel.hidden = !(you.role === "guesser" && room.phase === "running");
  renderMembership(room, you);
  spectators.textContent = room.spectators;
  statRound.textContent = room.round;
  statScore.textContent = room.score;
  statCorrect.textContent = room.correct;
  statWrong.textContent = room.wrong;
  statPasses.textContent = `${room.passes} / 3`;
  statDoubles.textContent = `${room.doubles} / 2`;
  renderRecords(room.records);
  playerList.replaceChildren(...room.players.map((player, index) => {
    const item = document.createElement("li");
    const seat = document.createElement("span");
    const playerName = document.createElement("strong");
    seat.textContent = index + 1;
    const playerLabel = player
      ? `${player.name} — ${roleLabels[player.turn_role] || "Giocatore"}`
      : "Posto libero";
    playerName.textContent = player?.connected === false ? `${playerLabel} — disconnesso (30 s)` : playerLabel;
    item.classList.toggle("is-disconnected", player?.connected === false);
    item.append(seat, playerName);
    return item;
  }));
  actionButtons.forEach((button) => {
    button.disabled = Date.now() < actionCooldownUntil || !canUse(button.dataset.action, room, you);
  });
}

function sendAction(action) {
  if (Date.now() < actionCooldownUntil) return;
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    setNotice("Connessione non disponibile.");
    return;
  }
  actionCooldownUntil = Date.now() + 1000;
  clearTimeout(actionCooldownTimer);
  actionCooldownTimer = setTimeout(() => {
    actionCooldownUntil = 0;
    if (roomState) render();
  }, 1000);
  if (roomState) render();
  socket.send(JSON.stringify({ type: "action", action }));
}

function sendMembership(type, seat) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    setNotice("Connessione non disponibile.");
    return;
  }
  if (type === "claim-seat") pendingSeat = seat;
  socket.send(JSON.stringify({ type, ...(seat ? { seat } : {}) }));
}

function updateRule() {
  ruleImage.src = `/assets/img/${rules[currentRule]}`;
  ruleImage.alt = `Istruzioni di gioco, pagina ${currentRule + 1}`;
  rulePage.textContent = `${currentRule + 1} / ${rules.length}`;
  previousRule.disabled = currentRule === 0;
  nextRule.disabled = currentRule === rules.length - 1;
}

joinForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const name = nameInput.value.trim();
  if (!name) return;
  const initialRole = selectedInitialRole();
  if (socket?.readyState === WebSocket.OPEN && !roomState) {
    joinedName = name;
    joinedInitialRole = initialRole;
    sendJoin();
  } else {
    connect(name, initialRole);
  }
});

actionButtons.forEach((button) => button.addEventListener("click", () => sendAction(button.dataset.action)));
spectateButton.addEventListener("click", () => sendMembership("spectate"));
helpButton.addEventListener("click", () => { playSound("click"); rulesDialog.showModal(); });
previousRule.addEventListener("click", () => { currentRule -= 1; updateRule(); });
nextRule.addEventListener("click", () => { currentRule += 1; updateRule(); });

window.addEventListener("keydown", (event) => {
  const target = event.target;
  if (target instanceof HTMLElement && target.closest("input, textarea, select, button, dialog")) return;
  if (!roomState || event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
  const action = {
    Space: "space",
    Enter: "correct",
    Backspace: "wrong",
    KeyP: "pass",
    KeyR: "double",
    KeyN: "next-round",
    KeyF: "finish",
  }[event.code];
  if (!action) return;
  if (canUse(action, roomState.room, roomState.you)) {
    event.preventDefault();
    sendAction(action);
  }
});

document.addEventListener("pointerdown", unlockSounds, { once: true });
window.addEventListener("keydown", unlockSounds, { once: true });

updateRule();
