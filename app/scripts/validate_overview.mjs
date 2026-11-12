import { spawn } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const scriptDir = dirname(fileURLToPath(import.meta.url));
const htmlPath = resolve(scriptDir, "..", "..", "data", "书库总览.html");
const sourceArg = process.argv[2] || htmlPath;
const dynamicMode = /^https?:/i.test(sourceArg);
const port = 9333;
const profile = join(process.env.TEMP || process.env.TMP || ".", "book-library-validator-chrome");
const url = dynamicMode ? sourceArg : pathToFileURL(sourceArg).href;

const chrome = spawn(
  chromePath,
  [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    "--window-size=1440,1000",
    url,
  ],
  { stdio: "ignore", windowsHide: true },
);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitForTarget() {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/json/list`);
      const targets = await response.json();
      const target = targets.find((item) => item.type === "page");
      if (target) return target;
    } catch {
      // Chrome is still starting.
    }
    await sleep(200);
  }
  throw new Error("Chrome DevTools endpoint did not start");
}

function connect(webSocketUrl) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(webSocketUrl);
    const pending = new Map();
    const exceptions = [];
    const navigations = [];
    const contexts = [];
    let nextId = 1;
    socket.addEventListener("open", () => {
      resolve({
        socket,
        exceptions,
        navigations,
        contexts,
        mainFrameId: "",
        send(method, params = {}) {
          const id = nextId;
          nextId += 1;
          socket.send(JSON.stringify({ id, method, params }));
          return new Promise((resolveMessage, rejectMessage) => {
            pending.set(id, { resolve: resolveMessage, reject: rejectMessage });
          });
        },
      });
    });
    socket.addEventListener("error", reject);
    socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) {
        const handler = pending.get(message.id);
        pending.delete(message.id);
        if (message.error) handler.reject(new Error(message.error.message));
        else handler.resolve(message.result);
      }
      if (message.method === "Runtime.exceptionThrown") {
        exceptions.push(message.params.exceptionDetails.text);
      }
      if (message.method === "Runtime.executionContextCreated") {
        contexts.push(message.params.context);
      }
      if (message.method === "Runtime.executionContextDestroyed") {
        const index = contexts.findIndex(
          (context) => context.id === message.params.executionContextId
        );
        if (index >= 0) contexts.splice(index, 1);
      }
      if (message.method === "Page.frameNavigated" && !message.params.frame.parentId) {
        navigations.push(message.params.frame.url);
      }
    });
  });
}

async function evaluate(client, expression) {
  let response;
  try {
    const context = client.contexts.findLast(
      (item) => item.auxData?.frameId === client.mainFrameId && item.auxData?.isDefault
    );
    response = await client.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
      ...(context ? { contextId: context.id } : {}),
    });
  } catch (error) {
    console.error(`Evaluate failed: ${expression.trim().slice(0, 220).replace(/\s+/g, " ")}`);
    console.error(`Top-level navigations: ${client.navigations.join(" -> ")}`);
    throw error;
  }
  if (response.exceptionDetails) {
    throw new Error(
      response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text
    );
  }
  return response.result.value;
}

async function inspectViewport(client, width, height, mobile) {
  await client.send("Emulation.setDeviceMetricsOverride", {
    width,
    height,
    deviceScaleFactor: 1,
    mobile,
  });
  await sleep(250);
  return evaluate(
    client,
    `(() => {
      const body = document.body;
      const toolbar = document.querySelector(".toolbar");
      const table = document.querySelector(".table-wrap");
      const search = document.getElementById("search");
      const controls = [...document.querySelectorAll("input, select, button")];
      const covers = [...document.querySelectorAll(".cover")];
      const externalResources = [...document.querySelectorAll("link[href], script[src]")]
        .map((element) => element.href || element.src)
        .filter((url) => {
          if (!/^https?:/i.test(url)) return false;
          try {
            return !["127.0.0.1", "localhost"].includes(new URL(url).hostname);
          } catch {
            return true;
          }
        });
      const overflowingControls = controls
        .filter((element) => element.scrollWidth > element.clientWidth + 1)
        .map((element) => element.id || element.textContent.trim());
      const toolbarStyle = getComputedStyle(toolbar);
      const buttonGeometry = Object.fromEntries(
        [".view-toggle button", ".format-tab", ".library-access button", ".path-actions button", ".card-actions button", ".collection-create button", ".collection-tab", ".selection-bar button", ".theme-toggle"]
          .map((selector) => [
            selector,
            [...document.querySelectorAll(selector)].slice(0, 8).map((button) => {
              const rect = button.getBoundingClientRect();
              const style = getComputedStyle(button);
              return {
                text: button.textContent.trim(),
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                w: Math.round(rect.width),
                h: Math.round(rect.height),
                transform: style.transform,
                align: style.alignItems,
                lineHeight: style.lineHeight,
                padding: style.padding,
              };
            }),
          ])
      );
      return {
        readyState: document.readyState,
        serverMode: Boolean(SERVER_MODE),
        bridgeMode: Boolean(BRIDGE_MODE),
        dataCount: DATA.length,
        renderedRows: document.querySelectorAll("#rows tr").length,
        renderedItems: document.querySelectorAll(".book-card").length || document.querySelectorAll("#rows tr").length,
        countText: document.getElementById("count").textContent,
        bodyWidth: body.scrollWidth,
        viewportWidth: innerWidth,
        horizontalBodyOverflow: body.scrollWidth > innerWidth + 1,
        tableClientWidth: table.clientWidth,
        tableScrollWidth: table.scrollWidth,
        toolbarColumns: toolbarStyle.gridTemplateColumns,
        searchHeight: search.getBoundingClientRect().height,
        languageOptions: [...document.querySelectorAll("#language option")]
          .map((option) => option.textContent.trim()),
        authorFilter: Boolean(document.getElementById("author")),
        shelfOptions: [...document.querySelectorAll("#shelf option")]
          .map((option) => option.textContent.trim()),
        tagOptions: [...document.querySelectorAll("#tag option")]
          .map((option) => option.textContent.trim()),
        smartSubcategoryOptions: [...document.querySelectorAll("#smart-subcategory option")]
          .map((option) => option.textContent.trim()),
        formatTabs: [...document.querySelectorAll(".format-tab[data-format]")]
          .map((button) => button.textContent.trim()),
        authorStatsButton: Boolean(document.getElementById("author-stats-open")),
        pdfTabCount: Number(
          document.querySelector('.format-tab[data-format="PDF"] span')?.textContent.replace(/,/g, "")
        ),
        ratingOptions: [...document.querySelectorAll("#rating option")]
          .map((option) => option.textContent.trim()),
        sizeSortOptions: [...document.querySelectorAll("#size-sort option")]
          .map((option) => option.textContent.trim()),
        collectionControls: Boolean(
          document.getElementById("collection-tabs") &&
          document.getElementById("collection-name") &&
          document.getElementById("create-collection")
        ),
        collectionTabs: [...document.querySelectorAll("#collection-tabs .collection-tab")]
          .map((button) => button.textContent.trim()),
        themeToggle: Boolean(document.getElementById("theme-toggle")),
        selectionCheckboxes: document.querySelectorAll(".select-book").length,
        fileSizes: [...document.querySelectorAll(".file-size")].slice(0, 5)
          .map((element) => element.textContent.trim()),
        copyButtons: document.querySelectorAll(".copy-path").length,
        openButtons: document.querySelectorAll(".open-book").length,
        revealButtons: document.querySelectorAll(".reveal-book").length,
        doubanButtons: document.querySelectorAll(".douban-search").length,
        deleteButtons: document.querySelectorAll(".delete-open").length,
        listAiGenerates: document.querySelectorAll("#rows .ai-generate").length,
        listAiSummaries: document.querySelectorAll("#rows .list-ai").length,
        aiSummaryCache: AI_SUMMARIES.size,
        sampleAiKeys: [...AI_SUMMARIES.keys()].slice(0, 3),
        sampleItemKeys: DATA.slice(0, 3).map((item) => aiKey(item)),
        refreshAiButton: Boolean(document.getElementById("refresh-ai")),
        duplicateLinks: document.querySelectorAll(".dup-link").length,
        syncButton: document.getElementById("sync-library")?.textContent || "",
        syncStatus: document.getElementById("sync-status")?.textContent || "",
        connectButtonHidden: document.getElementById("connect-library")?.hidden ?? false,
        firstCopyPath: document.querySelector(".copy-path")?.dataset.path || "",
        coverImages: covers.length,
        loadedCoverImages: covers.filter((image) => image.complete && image.naturalWidth > 0).length,
        externalResources,
        overflowingControls,
        buttonGeometry,
      };
    })()`,
  );
}

try {
  const target = await waitForTarget();
  const client = await connect(target.webSocketDebuggerUrl);
  await client.send("Runtime.enable");
  await client.send("Page.enable");
  const frameTree = await client.send("Page.getFrameTree");
  client.mainFrameId = frameTree.frameTree.frame.id;
  await sleep(1200);

  const desktop = await inspectViewport(client, 1440, 1000, false);
  const stabilityWait = Number(process.env.STABLE_WAIT_MS || 0);
  if (stabilityWait > 0) {
    const originBefore = await evaluate(client, "performance.timeOrigin");
    await sleep(stabilityWait);
    const originAfter = await evaluate(client, "performance.timeOrigin");
    if (originBefore !== originAfter) {
      throw new Error("Page reloaded during stability window");
    }
  }
  const search = await evaluate(
    client,
    `(() => {
      const input = document.getElementById("search");
      input.value = "机器学习";
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return {
        countText: document.getElementById("count").textContent,
        rows: document.querySelectorAll("#rows tr").length,
      };
    })()`,
  );
  const reset = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      return document.getElementById("count").textContent;
    })()`,
  );
  const sizeSortTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const select = document.getElementById("size-sort");
      select.value = "size-desc";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      const desc = state.filtered.slice(0, 2).map((item) => Number(item.sz));
      select.value = "size-asc";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      const asc = state.filtered.slice(0, 2).map((item) => Number(item.sz));
      document.getElementById("reset").click();
      return {
        desc: desc[0] >= desc[1],
        asc: asc[0] <= asc[1],
        options: [...select.options].map((option) => option.textContent.trim()),
      };
    })()`,
  );
  const collectionTabsTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const tabs = [...document.querySelectorAll("#collection-tabs .collection-tab")];
      const all = tabs[0];
      const other = tabs[1];
      let selected = "全部书集";
      let filtered = false;
      let countText = document.getElementById("count").textContent;
      if (other) {
        other.click();
        selected = document.querySelector("#collection-tabs .collection-tab.active")?.textContent.trim() || "";
        filtered = !selected.startsWith("全部书集");
        countText = document.getElementById("count").textContent;
        document.querySelector("#collection-tabs .collection-tab")?.click();
      }
      return {
        labels: tabs.map((button) => button.textContent.trim()),
        allActive: document.querySelector("#collection-tabs .collection-tab.active")?.textContent.trim() === "全部书集",
        selected,
        filtered,
        countText,
      };
    })()`,
  );
  const selectionTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      selectedPaths.clear();
      render();
      const checkbox = document.querySelector(".select-book");
      checkbox.click();
      const oneSelected = selectedPaths.size;
      const barVisible = !document.getElementById("selection-bar").hidden;
      document.getElementById("select-page").click();
      const pageSelected = selectedPaths.size;
      document.getElementById("bulk-delete").click();
      const modal = {
        open: !document.getElementById("delete-modal").hidden,
        title: document.getElementById("delete-modal-title").textContent,
        disabled: document.getElementById("confirm-delete").disabled,
      };
      document.getElementById("cancel-delete").click();
      document.getElementById("bulk-clear").click();
      return { oneSelected, barVisible, pageSelected, modal, cleared: selectedPaths.size };
    })()`,
  );
  const themeTest = await evaluate(
    client,
    `(() => {
      const root = document.documentElement;
      const before = root.dataset.theme;
      document.getElementById("theme-toggle").click();
      const after = root.dataset.theme;
      const label = document.getElementById("theme-toggle").textContent;
      if (after === before) document.getElementById("theme-toggle").click();
      return { before, after, changed: before !== after, label };
    })()`,
  );
  const duplicateFilter = await evaluate(
    client,
    `(() => {
      const select = document.getElementById("duplicate");
      select.value = "yes";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return document.getElementById("count").textContent;
    })()`,
  );
  const languageFilter = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const select = document.getElementById("language");
      select.value = "英文";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return document.getElementById("count").textContent;
    })()`,
  );
  const formatTab = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const tab = document.querySelector('.format-tab[data-format="PDF"]');
      tab.click();
      return {
        countText: document.getElementById("count").textContent,
        active: tab.classList.contains("active"),
      };
    })()`,
  );
  const authorFilter = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const option = DATA.find((item) => item.a);
      const input = document.getElementById("author");
      input.value = option.a;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      return {
        author: option.a,
        countText: document.getElementById("count").textContent,
        rows: document.querySelectorAll("#rows tr").length,
      };
    })()`,
  );
  const smartSubcategoryTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const shelf = document.getElementById("shelf");
      shelf.value = "科学与自然";
      shelf.dispatchEvent(new Event("change", { bubbles: true }));
      const sub = document.getElementById("smart-subcategory");
      const options = [...sub.options].map((option) => option.textContent.trim());
      const physics = [...sub.options].find((option) => option.value === "物理学");
      if (physics) {
        sub.value = "物理学";
        sub.dispatchEvent(new Event("change", { bubbles: true }));
      }
      return {
        options,
        countText: document.getElementById("count").textContent,
        selected: sub.value,
      };
    })()`,
  );
  const authorStatsTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      document.getElementById("author-stats-open").click();
      const rows = [...document.querySelectorAll(".author-stat-row")];
      const first = rows[0];
      const last = rows[rows.length - 1];
      const firstCount = Number(first.querySelector(".author-stat-count").textContent.replace(/[^0-9]/g, ""));
      const lastCount = Number(last.querySelector(".author-stat-count").textContent.replace(/[^0-9]/g, ""));
      first.querySelector("button").click();
      return {
        rows: rows.length,
        sorted: firstCount >= lastCount,
        closed: document.getElementById("author-stats-modal").hidden,
        filteredAuthor: document.getElementById("author").value,
        countText: document.getElementById("count").textContent,
      };
    })()`,
  );
  const cardViewTest = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      document.getElementById("card-view").click();
      const cards = [...document.querySelectorAll(".book-card")];
      const title = document.querySelector(".card-title");
      return {
        cards: cards.length,
        cardsVisible: !document.getElementById("card-grid").hidden,
        tableHidden: document.getElementById("table-wrap").hidden,
        lineClamp: getComputedStyle(title).webkitLineClamp,
        doubanRatings: document.querySelectorAll(".card-douban").length,
        openButtons: document.querySelectorAll(".card-actions .open-book").length,
        aiButtons: document.querySelectorAll(".card-actions .ai-generate").length,
      };
    })()`,
  );
  const doubleClickTest = await evaluate(
    client,
    `(async () => {
      const previous = openBook;
      let openedPath = "";
      openBook = async (path) => { openedPath = path; };
      document.querySelector(".card-title").dispatchEvent(
        new MouseEvent("dblclick", { bubbles: true })
      );
      await new Promise((resolve) => setTimeout(resolve, 20));
      openBook = previous;
      return openedPath;
    })()`,
  );
  await client.send("Network.enable");
  await client.send("Network.setBlockedURLs", {
    urls: ["*search.douban.com*"],
  });
  const coverClickTest = await evaluate(
    client,
    `(async () => {
      const cover = document.querySelector(".card-cover, .card-cover-placeholder");
      cover.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
      await new Promise((resolve) => setTimeout(resolve, 420));
      const visible = !document.getElementById("douban-drawer").hidden;
      const src = document.getElementById("douban-frame").src;
      document.getElementById("douban-drawer-close").click();
      return { visible, src };
    })()`,
  );
  const coverDoubleClickTest = await evaluate(
    client,
    `(async () => {
      const previous = openBook;
      let openedPath = "";
      openBook = async (path) => { openedPath = path; };
      const cover = document.querySelector(".card-cover, .card-cover-placeholder");
      cover.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 1 }));
      cover.dispatchEvent(new MouseEvent("click", { bubbles: true, detail: 2 }));
      cover.dispatchEvent(new MouseEvent("dblclick", { bubbles: true, detail: 2 }));
      openBook = previous;
      return {
        openedPath,
        drawerHidden: document.getElementById("douban-drawer").hidden,
      };
    })()`,
  );
  const doubanDrawerTest = await evaluate(
    client,
    `(async () => {
      document.querySelector(".douban-search").click();
      await new Promise((resolve) => setTimeout(resolve, 50));
      const drawer = document.getElementById("douban-drawer");
      const frame = document.getElementById("douban-frame");
      const result = {
        visible: !drawer.hidden,
        src: frame.src,
        fallback: document.getElementById("douban-new-window").href,
        sandbox: frame.getAttribute("sandbox"),
        pageUrl: location.href,
      };
      document.getElementById("douban-drawer-close").click();
      return result;
    })()`,
  );
  await client.send("Network.setBlockedURLs", { urls: [] });
  const ratingFilter = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const select = document.getElementById("rating");
      select.value = "9";
      select.dispatchEvent(new Event("change", { bubbles: true }));
      return document.getElementById("count").textContent;
    })()`,
  );
  const duplicateGroup = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const tag = document.querySelector(".dup-link");
      if (!tag) return null;
      const group = tag.dataset.group;
      tag.click();
      return {
        group,
        countText: document.getElementById("count").textContent,
        activeText: document.getElementById("active-group-text").textContent,
        rows: document.querySelectorAll("#rows tr").length,
      };
    })()`,
  );
  const modalAndHide = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      const before = DATA.length;
      document.querySelector(".delete-open").click();
      const modalOpen = !document.getElementById("delete-modal").hidden;
      const pathText = document.getElementById("delete-modal-path").textContent;
      const ack = document.getElementById("delete-ack");
      ack.checked = true;
      ack.dispatchEvent(new Event("change", { bubbles: true }));
      const deleteEnabled = !document.getElementById("confirm-delete").disabled;
      document.getElementById("hide-record").click();
      const afterHide = DATA.length;
      const restoreVisible = !document.getElementById("restore-hidden").hidden;
      document.getElementById("restore-hidden").click();
      const afterRestore = DATA.length;
      return { before, modalOpen, pathText, deleteEnabled, afterHide, restoreVisible, afterRestore };
    })()`,
  );
  const recycleRequestTest = await evaluate(
    client,
    `(async () => {
      const oldFetch = globalThis.fetch;
      const oldBridge = BRIDGE_MODE;
      const oldServer = SERVER_MODE;
      let request = null;
      BRIDGE_MODE = true;
      SERVER_MODE = false;
      globalThis.fetch = async (url, options = {}) => {
        request = { url: String(url), body: JSON.parse(options.body || "{}") };
        return new Response(JSON.stringify({ ok: true, paths: request.body.paths }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      };
      try {
        await recycleLibraryFiles(["test-folder\\\\book.epub"]);
        return request;
      } finally {
        globalThis.fetch = oldFetch;
        BRIDGE_MODE = oldBridge;
        SERVER_MODE = oldServer;
      }
    })()`,
  );
  const fakeSync = dynamicMode
    ? { result: null, paths: [], movedTitle: "" }
    : await evaluate(
        client,
        `(async () => {
      const oldRaw = RAW_DATA;
      const oldData = DATA;
      const oldHidden = hiddenRecordPaths;
      const oldDeleted = deletedFilePaths;
      const oldWalk = walkLibraryFiles;
      const oldRoot = libraryRootHandle;
      try {
        RAW_DATA = [
          {
            t: "Moved Book", a: "", f: "EPUB", fl: "英文", l: "英文",
            s: "文学小说", c: "old", z: 1, d: "", g: "", dc: 0, q: "",
            r: null, rc: null, u: "", mt: "", cv: "",
            p: "old\\\\book.epub", ap: "X:\\\\Library\\\\old\\\\book.epub"
          },
          {
            t: "Removed Book", a: "", f: "PDF", fl: "英文", l: "英文",
            s: "综合与待归类", c: "old", z: 2, d: "", g: "", dc: 0, q: "",
            r: null, rc: null, u: "", mt: "", cv: "",
            p: "old\\\\removed.pdf", ap: "X:\\\\Library\\\\old\\\\removed.pdf"
          }
        ];
        DATA = RAW_DATA.slice();
        hiddenRecordPaths = new Set();
        deletedFilePaths = new Set();
        libraryRootHandle = {
          name: "fake-root",
          queryPermission: async () => "granted",
          requestPermission: async () => "granted"
        };
        walkLibraryFiles = async () => [
          { relativePath: "new\\\\book.epub", name: "book.epub", size: 1048576, lastModified: 1 },
          { relativePath: "new\\\\added.epub", name: "added.epub", size: 2097152, lastModified: 2 }
        ];
        const result = await syncLibrary({ interactive: false, silent: true });
        return {
          result,
          paths: RAW_DATA.map((item) => item.p),
          movedTitle: RAW_DATA.find((item) => item.p.endsWith("book.epub"))?.t || "",
        };
      } finally {
        RAW_DATA = oldRaw;
        DATA = oldData;
        hiddenRecordPaths = oldHidden;
        deletedFilePaths = oldDeleted;
        walkLibraryFiles = oldWalk;
        libraryRootHandle = oldRoot;
        rebuildGroupCounts();
        applyFilters();
        updateSummary();
      }
    })()`,
      );
  const copyPoint = await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      globalThis.copyPathOriginal = copyPath;
      copyPath = async () => showToast("路径已复制");
      const rect = document.querySelector(".copy-path").getBoundingClientRect();
      return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
    })()`,
  );
  await client.send("Input.dispatchMouseEvent", {
    type: "mouseMoved",
    x: copyPoint.x,
    y: copyPoint.y,
  });
  await client.send("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: copyPoint.x,
    y: copyPoint.y,
    button: "left",
    clickCount: 1,
  });
  await client.send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: copyPoint.x,
    y: copyPoint.y,
    button: "left",
    clickCount: 1,
  });
  await sleep(200);
  const copyToast = await evaluate(
    client,
    `document.getElementById("toast").textContent`,
  );
  await evaluate(
    client,
    `(() => {
      copyPath = globalThis.copyPathOriginal;
      delete globalThis.copyPathOriginal;
    })()`,
  );
  await evaluate(
    client,
    `(() => {
      document.getElementById("reset").click();
      document.getElementById("next").click();
      return document.getElementById("page").textContent;
    })()`,
  );
  const pager = await evaluate(client, `document.getElementById("page").textContent`);
  const mobile = await inspectViewport(client, 390, 844, true);

  const report = {
    desktop,
    search,
    reset,
    sizeSortTest,
    collectionTabsTest,
    selectionTest,
    themeTest,
    duplicateFilter,
    languageFilter,
    formatTab,
    authorFilter,
    smartSubcategoryTest,
    authorStatsTest,
    cardViewTest,
    doubleClickTest,
    coverClickTest,
    coverDoubleClickTest,
    doubanDrawerTest,
    ratingFilter,
    duplicateGroup,
    modalAndHide,
    recycleRequestTest,
    fakeSync,
    copyToast,
    pager,
    mobile,
    runtimeExceptions: client.exceptions,
  };
  console.log(JSON.stringify(report, null, 2));

  const failures = [];
  if (desktop.dataCount !== 14300) failures.push("desktop data count");
  if (desktop.renderedRows !== 100) failures.push("desktop initial page size");
  if (desktop.horizontalBodyOverflow) failures.push("desktop body overflow");
  if (desktop.overflowingControls.length) failures.push("desktop control overflow");
  if (!desktop.languageOptions.includes("中文") || !desktop.languageOptions.includes("英文")) {
    failures.push("language options");
  }
  if (!desktop.ratingOptions.includes("9 分及以上")) failures.push("rating options");
  if (
    !desktop.sizeSortOptions.some((value) => value.includes("文件大小")) ||
    !desktop.fileSizes.length ||
    desktop.selectionCheckboxes !== 100 ||
    !desktop.collectionControls ||
    !desktop.themeToggle
  ) {
    failures.push("size, selection, collections or theme controls");
  }
  if (
    !desktop.collectionTabs.includes("全部书集") ||
    !collectionTabsTest.allActive ||
    (collectionTabsTest.labels.length > 1 &&
      (!collectionTabsTest.filtered || collectionTabsTest.countText === reset))
  ) {
    failures.push("expanded collection tabs");
  }
  if (desktop.copyButtons !== 100) failures.push("copy buttons");
  const expectedOpenButtons = desktop.serverMode || desktop.bridgeMode ? 100 : 0;
  if (desktop.openButtons !== expectedOpenButtons) failures.push("open buttons");
  if (desktop.revealButtons !== 100) failures.push("reveal buttons");
  if (desktop.doubanButtons !== 100) failures.push("douban buttons");
  if (desktop.deleteButtons !== 100) failures.push("delete buttons");
  if (!desktop.authorFilter) failures.push("author filter");
  if (!desktop.authorStatsButton) failures.push("author stats button");
  if (!desktop.refreshAiButton) failures.push("refresh AI button");
  if (desktop.shelfOptions.length < 10 || desktop.tagOptions.length < 5) {
    failures.push("searchable classification options");
  }
  if (desktop.formatTabs.length !== 7) failures.push("format tabs");
  if (!/^[A-Z]:\\/i.test(desktop.firstCopyPath)) failures.push("absolute copy path");
  if (desktop.coverImages === 0 || desktop.loadedCoverImages === 0) {
    failures.push("cover rendering");
  }
  if (desktop.externalResources.length) failures.push("external resources");
  if (search.countText === reset) failures.push("search did not filter");
  if (!sizeSortTest.desc || !sizeSortTest.asc) failures.push("size sorting");
  if (
    selectionTest.oneSelected !== 1 ||
    !selectionTest.barVisible ||
    selectionTest.pageSelected !== 100 ||
    !selectionTest.modal.open ||
    !selectionTest.modal.title.includes("100") ||
    selectionTest.cleared !== 0
  ) {
    failures.push("multi-select operations");
  }
  if (!themeTest.changed || !["深色", "浅色"].includes(themeTest.label)) {
    failures.push("dark mode");
  }
  if (duplicateFilter === reset) failures.push("duplicate filter");
  if (languageFilter === reset) failures.push("language filter");
  if (formatTab.countText !== `${desktop.pdfTabCount.toLocaleString()} 条记录` || !formatTab.active) {
    failures.push("PDF format tab");
  }
  if (authorFilter.countText === reset || !authorFilter.rows) failures.push("author filter result");
  if (
    smartSubcategoryTest.selected !== "物理学" ||
    !smartSubcategoryTest.options.some((value) => value.startsWith("物理学 (")) ||
    smartSubcategoryTest.countText === reset
  ) {
    failures.push("smart subcategory filter");
  }
  if (
    authorStatsTest.rows === 0 ||
    !authorStatsTest.sorted ||
    !authorStatsTest.closed ||
    !authorStatsTest.filteredAuthor ||
    authorStatsTest.countText === reset
  ) {
    failures.push("author statistics");
  }
  if (
    !cardViewTest.cardsVisible ||
    !cardViewTest.tableHidden ||
    cardViewTest.cards !== 100 ||
    cardViewTest.lineClamp !== "2" ||
    cardViewTest.doubanRatings !== 100 ||
    cardViewTest.openButtons !== expectedOpenButtons
  ) {
    failures.push("card view");
  }
  if (desktop.bridgeMode && desktop.aiSummaryCache === 0) failures.push("AI cache");
  if (!doubleClickTest) failures.push("card double-click open");
  if (
    !coverClickTest.visible ||
    !coverClickTest.src.includes("search.douban.com/book/subject_search")
  ) {
    failures.push("cover single-click Douban");
  }
  if (!coverDoubleClickTest.openedPath || !coverDoubleClickTest.drawerHidden) {
    failures.push("cover double-click open");
  }
  if (
    !doubanDrawerTest.visible ||
    !doubanDrawerTest.src.includes("search.douban.com/book/subject_search") ||
    !doubanDrawerTest.src.includes("cat=1001") ||
    !doubanDrawerTest.fallback.includes("search.douban.com/book/subject_search") ||
    doubanDrawerTest.sandbox.includes("allow-top-navigation") ||
    !doubanDrawerTest.pageUrl.includes("/data/%E4%B9%A6%E5%BA%93%E6%80%BB%E8%A7%88.html")
  ) {
    failures.push("douban drawer");
  }
  if (
    desktop.bridgeMode &&
    desktop.listAiGenerates === 0 &&
    desktop.listAiSummaries === 0
  ) {
    failures.push("list AI");
  }
  if (ratingFilter === reset) failures.push("rating filter");
  if (desktop.duplicateLinks > 0 && (
    !duplicateGroup || !duplicateGroup.activeText.includes(duplicateGroup.group)
  )) {
    failures.push("duplicate group filter");
  }
  if (
    !modalAndHide.modalOpen ||
    !/^[A-Z]:\\/i.test(modalAndHide.pathText) ||
    modalAndHide.afterHide !== modalAndHide.before - 1 ||
    !modalAndHide.restoreVisible ||
    modalAndHide.afterRestore !== modalAndHide.before
  ) {
    failures.push("delete modal or hide/restore");
  }
  if (
    !recycleRequestTest ||
    !recycleRequestTest.url.includes("/api/recycle") ||
    !recycleRequestTest.body.paths?.includes("test-folder\\book.epub")
  ) {
    failures.push("recycle bin deletion path");
  }
  if (
    !dynamicMode &&
    (
      !fakeSync.paths.includes("new\\book.epub") ||
      !fakeSync.paths.includes("new\\added.epub") ||
      fakeSync.paths.includes("old\\removed.pdf") ||
      fakeSync.movedTitle !== "Moved Book"
    )
  ) {
    failures.push("library auto-sync");
  }
  if (!desktop.syncButton.includes("刷新") && !desktop.syncButton.includes("同步")) {
    failures.push("refresh button");
  }
  if (dynamicMode && desktop.serverMode && !desktop.connectButtonHidden) {
    failures.push("server mode access UI");
  }
  if (dynamicMode && desktop.serverMode && !desktop.syncStatus.includes("动态服务")) {
    failures.push("server mode status");
  }
  if (dynamicMode && !desktop.serverMode && !desktop.syncStatus.includes("静态服务")) {
    failures.push("static service status");
  }
  if (copyToast !== "路径已复制") failures.push("copy feedback");
  if (!pager.startsWith("2 /")) failures.push("pagination");
  if (mobile.horizontalBodyOverflow) failures.push("mobile body overflow");
  if (mobile.renderedItems !== 100) failures.push("mobile item count");
  if (client.exceptions.length) failures.push("runtime exception");
  if (failures.length) {
    console.error(`Validation failed: ${failures.join(", ")}`);
    process.exitCode = 1;
  }
  client.socket.close();
} finally {
  chrome.kill();
}
