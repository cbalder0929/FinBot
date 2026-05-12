const form = document.getElementById('analyzer-form');
const fileInput = document.getElementById('statement-file');
const results = document.getElementById('results');
const progressText = document.getElementById('progress-text');
const robot = document.getElementById('robot');
const transactionRows = document.getElementById('transaction-rows');

const summaryTargets = {
  count: document.getElementById('transaction-count'),
  credits: document.getElementById('credits'),
  debits: document.getElementById('debits'),
  net: document.getElementById('net'),
};

const statusFrames = [
  'Reading uploaded statement…',
  'Extracting transactions…',
  'Asking Ollama to categorize entries…',
  'Building structured financial summary…',
];

function formatCurrency(value) {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
  }).format(value);
}

function setRobotState(state) {
  robot.className = `robot ${state}`;
}

function renderTransactions(data) {
  summaryTargets.count.textContent = String(data.transactionCount);
  summaryTargets.credits.textContent = formatCurrency(data.totals.credits);
  summaryTargets.debits.textContent = formatCurrency(data.totals.debits);
  summaryTargets.net.textContent = formatCurrency(data.totals.net);

  transactionRows.innerHTML = data.transactions
    .map(
      (transaction) => `
        <tr>
          <td>${transaction.date || '—'}</td>
          <td>${transaction.description}</td>
          <td>${transaction.category || 'general'}</td>
          <td>${transaction.direction}</td>
          <td>${formatCurrency(transaction.amount)}</td>
          <td>${Math.round((transaction.confidence || 0) * 100)}%</td>
        </tr>
      `,
    )
    .join('');

  results.classList.remove('hidden');
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (!fileInput.files.length) {
    return;
  }

  const payload = new FormData();
  payload.append('file', fileInput.files[0]);

  let frameIndex = 0;
  progressText.textContent = statusFrames[0];
  setRobotState('robot-working');

  const ticker = window.setInterval(() => {
    frameIndex = (frameIndex + 1) % statusFrames.length;
    progressText.textContent = statusFrames[frameIndex];
  }, 900);

  try {
    const response = await fetch('/api/analyze', {
      method: 'POST',
      body: payload,
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || 'Unable to analyze statement.');
    }

    renderTransactions(data);
    progressText.textContent = `Finished analyzing ${data.fileName}.`;
    setRobotState('robot-complete');
  } catch (error) {
    progressText.textContent = error.message;
    setRobotState('robot-idle');
  } finally {
    window.clearInterval(ticker);
    window.setTimeout(() => setRobotState('robot-idle'), 1800);
  }
});
