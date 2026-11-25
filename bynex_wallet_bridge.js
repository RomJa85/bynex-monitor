/*
 * BYNEX Monitor — передача общего баланса кошелька
 *
 * Запуск:
 * 1. Открой BYNEX -> Кошельки в авторизованной вкладке.
 * 2. DevTools -> Sources -> Snippets -> New snippet.
 * 3. Вставь этот файл и запусти.
 *
 * Читает только отображаемый общий баланс и выбранную валюту.
 * Токены, Cookie и другие данные авторизации не читает и не передаёт.
 */
(() => {
    "use strict";

    const API_URL =
        "https://desktop-rbesf9t.tail542c4c.ts.net/api/wallet-balance";

    const BALANCE_SELECTOR =
        '[data-testid="wallets.page_h3_total_balance_visibility_visibility"]';
    const CURRENCY_SELECTOR =
        '[data-testid="wallets.page_app-drop_currencylabel_wallet_currency_id"] app-drop-label';

    const ALLOWED_CURRENCIES = new Set(["BYN", "USD", "BTC"]);
    const HEARTBEAT_MS = 30_000;
    const MUTATION_DEBOUNCE_MS = 250;

    // Имитация пользовательской активности для штатного продления сессии BYNEX.
    const SESSION_ACTIVITY_BASE_MS = 45_000;
    const SESSION_ACTIVITY_RANDOM_MS = 15_000;

    // Состояние таймеров и отправки данных.
    let sending = false;
    let lastSignature = null;
    let lastSentAt = 0;
    let debounceTimer = null;
    let observer = null;
    let heartbeatTimer = null;
    let sessionActivityTimer = null;

    function readWalletBalance() {
        const balanceElement = document.querySelector(BALANCE_SELECTOR);
        const currencyElement = document.querySelector(CURRENCY_SELECTOR);
        if (!balanceElement || !currencyElement) return null;

        const paragraphs = balanceElement.querySelectorAll("p");
        const rawBalance = paragraphs[1]?.innerText?.trim();
        const currency = currencyElement.innerText.trim().toUpperCase();

        if (!rawBalance || !ALLOWED_CURRENCIES.has(currency)) return null;

        const normalized = rawBalance
            .replace(/[\s\u00a0\u202f]/g, "")
            .replace(",", ".");
        const balance = Number(normalized);

        if (!Number.isFinite(balance) || balance < 0) return null;
        return { currency, balance };
    }

    async function sendWalletBalance(force = false) {
        if (sending) return;
        const current = readWalletBalance();

        if (!current) {
            console.warn("[BYNEX Monitor] Не удалось прочитать общий баланс или валюту.");
            return;
        }

        const signature = current.currency + ":" + current.balance;
        const now = Date.now();

        if (!force && signature === lastSignature &&
            now - lastSentAt < HEARTBEAT_MS) return;

        sending = true;
        try {
            const response = await fetch(API_URL, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(current),
                cache: "no-store",
                credentials: "omit"
            });

            if (!response.ok) throw new Error("HTTP " + response.status);
            const result = await response.json();

            lastSignature = signature;
            lastSentAt = Date.now();

            console.info("[BYNEX Monitor] Баланс передан:",
                result.wallet_balance.currency,
                result.wallet_balance.balance);
        } catch (error) {
            console.error("[BYNEX Monitor] Ошибка передачи баланса:", error);
        } finally {
            sending = false;
        }
    }

    function scheduleSend() {
        if (debounceTimer !== null) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            debounceTimer = null;
            sendWalletBalance();
        }, MUTATION_DEBOUNCE_MS);
    }

    const balanceElement = document.querySelector(BALANCE_SELECTOR);
    const currencyElement = document.querySelector(CURRENCY_SELECTOR);

    if (!balanceElement || !currencyElement) {
        console.error("[BYNEX Monitor] Не найдены элементы общего баланса. " +
            "Открой страницу «Кошельки» и запусти Snippet повторно.");
        return;
    }

    function dispatchSessionActivity() {
        const target = document.body;
        if (target) {
            target.dispatchEvent(new MouseEvent("mousemove", {
                bubbles: true,
                cancelable: true,
                clientX: 500,
                clientY: 300,
                view: window
            }));
            console.info("[BYNEX Monitor] Событие активности отправлено.");
        }

        const delay = SESSION_ACTIVITY_BASE_MS +
            Math.floor(Math.random() * SESSION_ACTIVITY_RANDOM_MS) + 1;
        sessionActivityTimer = setTimeout(dispatchSessionActivity, delay);
    }

    // Первое событие отправляем сразу, следующие — через 46–60 секунд.
    dispatchSessionActivity();

    observer = new MutationObserver(scheduleSend);
    observer.observe(balanceElement, {
        childList: true, subtree: true, characterData: true
    });
    observer.observe(currencyElement, {
        childList: true, subtree: true, characterData: true
    });

    sendWalletBalance(true);
    heartbeatTimer = setInterval(() => sendWalletBalance(true), HEARTBEAT_MS);

    window.__bynexWalletBridgeStop = () => {
        if (observer) observer.disconnect();
        if (debounceTimer !== null) clearTimeout(debounceTimer);
        if (heartbeatTimer !== null) clearInterval(heartbeatTimer);
        if (sessionActivityTimer !== null) clearTimeout(sessionActivityTimer);
        observer = null;
        debounceTimer = null;
        heartbeatTimer = null;
        console.info("[BYNEX Monitor] Передача баланса остановлена.");
    };

    console.info("[BYNEX Monitor] Передача общего баланса запущена. " +
        "Остановка: window.__bynexWalletBridgeStop()");
})();
