const socket = io();
const statusEl = document.getElementById("status");
const tbody = document.querySelector("#suspects tbody");
const canvas = document.getElementById("waterfall");
const ctx = canvas.getContext("2d");

function setStatus(t) { statusEl.textContent = t; }

document.getElementById("baseline").onclick = () => {
  setStatus("capturing baseline...");
  socket.emit("capture_baseline", { cycles: 5 });
};
document.getElementById("start").onclick = () => { socket.emit("start", {}); setStatus("sweeping"); };
document.getElementById("stop").onclick = () => { socket.emit("stop", {}); setStatus("stopped"); };

socket.on("baseline_ready", (d) => setStatus(`baseline ready (${d.sweep_count} sweeps)`));
socket.on("error", (d) => setStatus("error: " + d.message));

// Color map: -110 dBm -> dark, -20 dBm -> bright.
function color(dbm) {
  const v = Math.max(0, Math.min(1, (dbm + 110) / 90));
  const r = Math.floor(255 * Math.min(1, v * 2));
  const g = Math.floor(255 * Math.max(0, v * 2 - 1));
  return `rgb(${r},${g},40)`;
}

socket.on("sweep", (d) => {
  // Scroll the waterfall down by 1px, draw the new row on top.
  ctx.drawImage(canvas, 0, 0, canvas.width, canvas.height - 1, 0, 1, canvas.width, canvas.height - 1);
  const n = d.powers.length;
  for (let x = 0; x < canvas.width; x++) {
    const p = d.powers[Math.floor((x / canvas.width) * n)];
    ctx.fillStyle = color(p);
    ctx.fillRect(x, 0, 1, 1);
  }
});

socket.on("findings", (d) => renderTable(d.findings));

function makeCell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function renderTable(findings) {
  tbody.innerHTML = "";
  for (const f of findings) {
    const tr = document.createElement("tr");
    tr.dataset.freq = f.freq_hz;
    tr.appendChild(makeCell(f.score, f.score >= 70 ? "hot" : ""));
    tr.appendChild(makeCell(f.freq_mhz.toFixed(3)));
    tr.appendChild(makeCell(f.band));
    tr.appendChild(makeCell(f.reasons.join(", ")));
    const btnTd = document.createElement("td");
    const btn = document.createElement("button");
    btn.textContent = "investigate";
    btn.onclick = () => {
      setStatus(`investigating ${f.freq_mhz.toFixed(3)} MHz...`);
      socket.emit("investigate", { freq_hz: f.freq_hz });
    };
    btnTd.appendChild(btn);
    tr.appendChild(btnTd);
    tbody.appendChild(tr);
  }
}

socket.on("finding", (f) => {
  const row = tbody.querySelector(`tr[data-freq="${f.freq_hz}"]`);
  if (!row) return;
  row.children[0].textContent = f.score;
  row.children[0].className = f.score >= 70 ? "hot" : "";
  row.children[3].textContent = f.reasons.join(", ");
  setStatus(`investigated ${f.freq_mhz.toFixed(3)} MHz: score ${f.score}`);
});
