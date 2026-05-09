let isRunning = false;
let logIndex = 0;
let pollInterval = null;
let lastSuccessCount = 0;
let lastInventoryCount = 0;
let lastAccountsRefreshAt = 0;
let accountsLoading = false;

// Khởi tạo
document.addEventListener('DOMContentLoaded', () => {
    switchTab('dashboard');
    startPolling();
});

// Chuyển chế độ xem
function switchTab(tabName) {
    document.querySelectorAll('.view-section').forEach(el => {
        el.classList.remove('active');
        el.classList.add('hidden');
    });
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));

    const targetView = document.getElementById(`view-${tabName}`);
    targetView.classList.remove('hidden');
    targetView.classList.add('active');

    // Tô sáng mục điều hướng tương ứng
    const navIndex = tabName === 'dashboard' ? 0 : 1;
    document.querySelectorAll('.nav-item')[navIndex].classList.add('active');

    if (tabName === 'accounts') {
        loadAccounts({ showLoading: true });
    }
}

// Thăm dò trạng thái
function startPolling() {
    pollStatus(); // Chạy ngay một lần
    pollInterval = setInterval(pollStatus, 1000);
}

async function pollStatus() {
    try {
        const res = await fetch(`/api/status?log_index=${logIndex}`);
        const data = await res.json();

        updateUI(data);
    } catch (e) {
        console.error("Polling error:", e);
    }
}

function updateUI(data) {
    // 1. Cập nhật chỉ số cơ bản
    document.getElementById('valAction').textContent = data.current_action;
    document.getElementById('valSuccess').textContent = data.success;
    document.getElementById('valFail').textContent = data.fail;
    document.getElementById('valInventory').textContent = data.total_inventory;
    const backendVersion = document.getElementById('backendVersion');
    if (backendVersion) {
        backendVersion.textContent = `Backend: ${data.version || 'unknown'} | ${data.started_at || ''}`;
    }
    const lastUpdate = document.getElementById('lastUpdate');
    if (lastUpdate) {
        lastUpdate.textContent = new Date().toLocaleTimeString();
    }

    const accountsViewActive = document.getElementById('view-accounts').classList.contains('active');
    const successChanged = Number(data.success || 0) !== lastSuccessCount;
    const inventoryChanged = Number(data.total_inventory || 0) !== lastInventoryCount;
    const shouldRefreshAccounts = (
        accountsViewActive
        && (successChanged || inventoryChanged || (isRunning && Date.now() - lastAccountsRefreshAt > 3000))
    );
    lastSuccessCount = Number(data.success || 0);
    lastInventoryCount = Number(data.total_inventory || 0);
    if (shouldRefreshAccounts) {
        loadAccounts({ silent: true });
    }

    // 2. Cập nhật trạng thái chạy
    isRunning = data.is_running;
    const btnStart = document.getElementById('btnStart');
    const btnStop = document.getElementById('btnStop');
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('statusText');

    if (isRunning) {
        btnStart.classList.add('hidden');
        btnStop.classList.remove('hidden');
        statusDot.classList.add('running');
        statusText.textContent = "Đang chạy";
    } else {
        btnStart.classList.remove('hidden');
        btnStop.classList.add('hidden');
        statusDot.classList.remove('running');
        statusText.textContent = "Hệ thống đang rảnh";
    }

    // 4. Cập nhật hình ảnh giám sát
    const monitorImg = document.getElementById('liveMonitor');
    const noSignal = document.getElementById('noSignal');
    const monitorStatus = document.getElementById('monitorStatus');

    if (isRunning) {
        monitorImg.classList.remove('hidden');
        noSignal.classList.add('hidden');

        // Chỉ gán src khi cần để tránh làm mới luồng liên tục gây nhấp nháy
        if (!monitorImg.src || monitorImg.src.indexOf('/video_feed') === -1) {
            monitorImg.src = "/video_feed";
        }

        monitorStatus.textContent = "LIVE";
        monitorStatus.classList.remove('neutral');
        monitorStatus.classList.add('success');
    } else {
        monitorStatus.textContent = "OFFLINE";
        monitorStatus.classList.remove('success');
        monitorStatus.classList.add('neutral');
        // Tác vụ đã kết thúc nhưng vẫn có thể giữ khung hình cuối cùng
        // Nếu muốn ngắt luồng: monitorImg.src = "";
    }

    // 5. Thêm nhật ký
    if (data.logs && data.logs.length > 0) {
        const container = document.getElementById('logContainer');

        // Xóa placeholder
        const placeholder = container.querySelector('.log-placeholder');
        if (placeholder) placeholder.remove();

        data.logs.forEach(logLine => {
            const div = document.createElement('div');
            div.className = 'log-entry';
            div.textContent = logLine;
            container.appendChild(div);
        });

        // Tự động cuộn xuống cuối
        container.scrollTop = container.scrollHeight;

        // Cập nhật chỉ mục để tránh kéo trùng nhật ký
        logIndex += data.logs.length;
    }
}

// Bắt đầu tác vụ
async function startTask() {
    const count = parseInt(document.getElementById('targetCount').value) || 1;

    // Xóa nhật ký cũ
    clearLogs();
    loadAccounts({ silent: true });

    try {
        const res = await fetch('/api/start', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: count })
        });

        if (!res.ok) {
            alert("Bắt đầu thất bại: " + await res.text());
        }
    } catch (e) {
        alert("Yêu cầu thất bại: " + e);
    }
}

// Dừng tác vụ
async function stopTask() {
    if (!confirm("Bạn có chắc muốn dừng tác vụ hiện tại không?")) return;

    try {
        await fetch('/api/stop', { method: 'POST' });
    } catch (e) {
        console.error(e);
    }
}

// Xóa nhật ký
function clearLogs() {
    document.getElementById('logContainer').innerHTML = '<div class="log-placeholder">Đang chờ tác vụ bắt đầu...</div>';
    logIndex = 0; // Lưu ý: backend có thể cần logic xóa nhật ký tương ứng, đây chỉ là reset frontend
    // Backend lấy nhật ký theo index, nên reset index có thể kéo lại nhật ký cũ còn lưu ở backend.
    // Cách tốt hơn là backend có API xóa nhật ký hoặc frontend tự giữ offset riêng.
    // Trong bản đơn giản này, chỉ xóa DOM để tạo cảm giác đã xóa hiển thị.
}

// Tải danh sách tài khoản
async function loadAccounts(options = {}) {
    if (accountsLoading) return;
    accountsLoading = true;

    const tbody = document.getElementById('accountTableBody');
    if (!options.silent) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center">Đang tải...</td></tr>';
    }

    try {
        const res = await fetch('/api/accounts');
        const accounts = await res.json();

        renderAccounts(accounts);
        lastAccountsRefreshAt = Date.now();
    } catch (e) {
        if (!options.silent) {
            tbody.innerHTML = `<tr><td colspan="4" style="text-align:center;color:red">Tải thất bại: ${escapeHtml(String(e))}</td></tr>`;
        }
    } finally {
        accountsLoading = false;
    }
}

function escapeHtml(value) {
    return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function compactText(value, head = 24, tail = 12) {
    const text = String(value || '');
    if (text.length <= head + tail + 3) return text;
    return `${text.slice(0, head)}...${text.slice(-tail)}`;
}

async function copyText(value, button) {
    try {
        await navigator.clipboard.writeText(value || '');
        if (button) {
            const original = button.textContent;
            button.textContent = 'Đã copy';
            button.classList.add('copied');
            setTimeout(() => {
                button.textContent = original;
                button.classList.remove('copied');
            }, 900);
        }
    } catch (e) {
        alert('Copy thất bại: ' + e);
    }
}

function renderAccounts(accounts) {
    const tbody = document.getElementById('accountTableBody');
    tbody.innerHTML = '';

    if (accounts.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:#666">Chưa có tài khoản hoàn tất toàn bộ flow</td></tr>';
        return;
    }

    accounts.forEach(acc => {
        const tr = document.createElement('tr');
        const bundle = acc.email_bundle || acc.email || '';
        const payLink = acc.pay_link || '';
        tr.innerHTML = `
            <td>
                <div class="account-cell">
                    <div class="account-main">${escapeHtml(acc.email || compactText(bundle, 28, 0))}</div>
                    <div class="account-sub">${escapeHtml(compactText(bundle, 32, 10))}</div>
                </div>
            </td>
            <td>
                <div class="link-cell">
                    <a href="${escapeHtml(payLink)}" target="_blank" rel="noopener noreferrer">${escapeHtml(compactText(payLink, 34, 14))}</a>
                </div>
            </td>
            <td>${escapeHtml(acc.time)}</td>
            <td class="row-actions">
                <button class="mini-copy-btn" onclick='copyText(${JSON.stringify(bundle)}, this)'>Copy mail</button>
                <button class="mini-copy-btn" onclick='copyText(${JSON.stringify(payLink)}, this)'>Copy link</button>
            </td>
        `;
        tbody.appendChild(tr);
    });

    // Lưu vào biến toàn cục để tìm kiếm
    window.allAccounts = accounts;
}

// Tìm kiếm tài khoản
function filterAccounts() {
    const term = document.getElementById('searchInput').value.toLowerCase();
    if (!window.allAccounts) return;

    const filtered = window.allAccounts.filter(acc =>
        (acc.email || '').toLowerCase().includes(term)
        || (acc.email_bundle || '').toLowerCase().includes(term)
        || (acc.pay_link || '').toLowerCase().includes(term)
    );
    renderAccounts(filtered);
}
