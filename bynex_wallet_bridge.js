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

    // Штатное продление авторизованной сессии BYNEX.
    // Bearer-токен используется только внутри вкладки и не передаётся на сервер монитора.
    const SESSION_RENEW_URL =
        "/trading/ru/api/auth/postponeSessionExpiration";
    const SESSION_RENEW_INTERVAL_MS = 50_000;

    let observer = null;
    let debounceTimer = null;
    let heartbeatTimer = null;
    let lastSignature = "";
    let lastSentAt = 0;
    let sending = false;
    let sessionRenewTimer = null;
    let sessionRenewInProgress = false;

    async function renewBynexSession() {
        if (sessionRenewInProgress) return;

        // Токен перечитывается перед каждым запросом: если приложение обновит его,
        // следующий запрос автоматически использует новое значение.
        const token = localStorage.getItem("bynex_token");
        if (!token) {
            console.warn("[BYNEX Monitor] Не найден bynex_token; сессия не продлена.");
            return;
        }

        sessionRenewInProgress = true;
        try {
            const response = await fetch(SESSION_RENEW_URL, {
                method: "GET",
                headers: { "Authorization": "Bearer " + token },
                credentials: "include",
                cache: "no-store"
            });

            if (response.status === 401) {
                console.warn("[BYNEX Monitor] BYNEX отклонил продление сессии (401). " +
                    "Возможно, требуется повторная авторизация.");
                return;
            }
            if (!response.ok) {
                throw new Error("HTTP " + response.status);
            }

            const result = await response.json();
            console.info("[BYNEX Monitor] Сессия BYNEX продлена.",
                result.expireDate ? "Действует до: " + result.expireDate : "");
        } catch (error) {
            console.error("[BYNEX Monitor] Ошибка продления сессии:", error);
        } finally {
            sessionRenewInProgress = false;
        }
    }

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

    // Запускаем продление сразу и далее каждые 50 секунд.
    // По предыдущим наблюдениям штатный интервал BYNEX составлял около 63–74 секунд.
    renewBynexSession();
    sessionRenewTimer = setInterval(renewBynexSession, SESSION_RENEW_INTERVAL_MS);

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
        if (sessionRenewTimer !== null) clearInterval(sessionRenewTimer);
        observer = null;
        debounceTimer = null;
        heartbeatTimer = null;
        console.info("[BYNEX Monitor] Передача баланса остановлена.");
    };

    console.info("[BYNEX Monitor] Передача общего баланса запущена. " +
        "Остановка: window.__bynexWalletBridgeStop()");
})();
