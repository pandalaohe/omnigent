/** Only the MV3 APIs this companion uses; no page-facing messaging surface. */
declare namespace chrome {
  interface Event<T extends (...args: never[]) => unknown> {
    addListener(listener: T): void;
    removeListener(listener: T): void;
  }
  namespace tabs {
    interface Tab {
      id?: number;
      windowId: number;
      url?: string;
      active: boolean;
    }
    function query(query: { active: true; windowId: number }): Promise<Tab[]>;
    function captureVisibleTab(windowId: number, options: { format: "png" }): Promise<string>;
    function sendMessage(
      tabId: number,
      message: unknown,
      options: { documentId: string },
    ): Promise<unknown>;
    const onActivated: Event<(info: { tabId: number; windowId: number }) => void>;
    const onUpdated: Event<(tabId: number, change: { status?: string; url?: string }) => void>;
    const onRemoved: Event<(tabId: number) => void>;
  }
  namespace action {
    const onClicked: Event<(tab: tabs.Tab) => void>;
  }
  namespace sidePanel {
    function open(options: { tabId: number }): Promise<void>;
  }
  namespace windows {
    function get(windowId: number): Promise<{ focused: boolean; id?: number }>;
    function update(windowId: number, options: { focused: true }): Promise<unknown>;
    const onFocusChanged: Event<(windowId: number) => void>;
  }
  namespace scripting {
    function executeScript(options: {
      target: { tabId: number; frameIds: [0] };
      world: "ISOLATED";
      files: string[];
    }): Promise<{ frameId: number; documentId: string; result?: unknown }[]>;
  }
  namespace storage.local {
    function setAccessLevel(options: { accessLevel: "TRUSTED_CONTEXTS" }): Promise<void>;
  }
  namespace runtime {
    const id: string;
    function getURL(path: string): string;
    interface MessageSender {
      id?: string;
      tab?: tabs.Tab;
      frameId?: number;
      documentId?: string;
      url?: string;
    }
    interface Port {
      name: string;
      sender?: MessageSender;
      postMessage(message: unknown): void;
      disconnect(): void;
      onMessage: Event<(message: unknown) => void>;
      onDisconnect: Event<() => void>;
    }
    function connect(options: { name: string }): Port;
    function sendMessage(message: unknown): Promise<unknown>;
    const onConnect: Event<(port: Port) => void>;
    const onMessage: Event<
      (
        message: unknown,
        sender: MessageSender,
        respond: (response: unknown) => void,
      ) => boolean | undefined
    >;
  }
}
