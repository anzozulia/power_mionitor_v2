/*
 * Power Monitor admin script: the one first-party script, a classic script loaded in <head>
 * without defer. Its first statement applies the stored sidebar rail flag to <html> before
 * first paint. The rest of its top level only registers listeners and touches nothing on the
 * page: alpine:init (the Alpine.data components, bound by x-data in the templates), the
 * delegated submit guard on document and the one pageshow listener (the guard's reset, then
 * the handlers components register with onPageshow).
 *
 * Rules (06-UI-SPEC Interaction Contract, R1, R4): every DOM write sets an attribute or
 * textContent; server values come only from data-* attributes; no browser storage beyond the
 * rail flag; every POST stays a native form submission. The page works without this script;
 * it only enhances it.
 */
(function () {
  "use strict";

  try {
    if (window.localStorage.getItem("powermon.sidebar.rail") === "1") {
      document.documentElement.setAttribute("data-rail", "collapsed");
    }
  } catch (error) {
    // Storage is blocked: the rail stays expanded.
  }

  var TOAST = '[data-testid="toast"]';
  var DISMISS = 'button[aria-label="Dismiss"]';

  // Removes a toast. When the focus was inside it, the focus moves to main (UI-12).
  function removeToast(toast) {
    var hadFocus = toast.contains(document.activeElement);
    toast.remove();
    if (hadFocus) {
      var main = document.getElementById("main");
      if (main) {
        main.focus();
      }
    }
  }

  // The first text node of an element's label (icon markup holds no text): the visible
  // label of a button, or the sr-only label of an icon-only button.
  function labelNode(element) {
    var walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
    for (var node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.textContent.trim() !== "") {
        return node;
      }
    }
    return null;
  }

  // A method can be bound twice, by a directive (@click="toggleDrawer") and by the
  // component's own listener. Both receive the same event; only the first call acts.
  var handledEvents = new WeakSet();

  function firstCall(event) {
    if (!event || typeof event !== "object") {
      return true;
    }
    if (handledEvents.has(event)) {
      return false;
    }
    handledEvents.add(event);
    return true;
  }

  function reveal(element) {
    if (element) {
      element.hidden = false;
    }
  }

  // The element matching selector at or above the event's target, or null.
  function closestTo(event, selector) {
    return event.target instanceof Element ? event.target.closest(selector) : null;
  }

  // Relative times (UI-11): a port of powermon/web/templatetags/timefmt.py. The age is the
  // difference of two instants, floored to the unit of its range; under 1 s or in the future
  // it is "just now". "now" is the client clock corrected by the server's own instant (the
  // body's data-now, then each status poll's generated_at), so a wrong client clock never
  // shifts the text (06-RESEARCH Pitfall 15).
  var MS_PER_SECOND = 1000;
  var AGE_FLOORS = { minute: 60, hour: 3600, hoursLimit: 172800, day: 86400 };
  var JUST_NOW = "just now";
  var UNIT_AGO = { s: " s ago", min: " min ago", h: " h ago", d: " d ago" };
  var UNIT_WORDS = { s: " s", min: " min", h: " h", d: " d" };
  var UNIT_COMPACT = { s: "s", min: "m", h: "h", d: "d" };
  var RELATIVE_REFRESH_MS = 15000;

  // Server clock minus client clock, in milliseconds.
  var clockSkew = 0;

  function serverNow() {
    return Date.now() + clockSkew;
  }

  function parseInstant(iso) {
    var instant = Date.parse(iso || "");
    return isNaN(instant) ? null : instant;
  }

  // Corrects the client clock by a server instant read just now; returns the instant.
  function syncClock(iso) {
    var instant = parseInstant(iso);
    if (instant !== null) {
      clockSkew = instant - Date.now();
    }
    return instant;
  }

  // The age of an instant at now (both in ms) as {count, unit}, unit s, min, h or d.
  function ageParts(instant, now) {
    var seconds = Math.floor((now - instant) / MS_PER_SECOND);
    if (seconds < 1) {
      return { count: 0, unit: "s" };
    }
    if (seconds < AGE_FLOORS.minute) {
      return { count: seconds, unit: "s" };
    }
    if (seconds < AGE_FLOORS.hour) {
      return { count: Math.floor(seconds / AGE_FLOORS.minute), unit: "min" };
    }
    if (seconds < AGE_FLOORS.hoursLimit) {
      return { count: Math.floor(seconds / AGE_FLOORS.hour), unit: "h" };
    }
    return { count: Math.floor(seconds / AGE_FLOORS.day), unit: "d" };
  }

  function isoAge(iso, now) {
    var instant = parseInstant(iso);
    return instant === null ? null : ageParts(instant, now);
  }

  // "just now", "12 s ago", "3 min ago", "5 h ago" or "2 d ago" (shell.relative).
  function relativeText(age) {
    return age.count === 0 && age.unit === "s" ? JUST_NOW : age.count + UNIT_AGO[age.unit];
  }

  // The sidebar cell's age: "12s", "3m", "5h", "2d" (shell.sb_cell).
  function compactAge(age) {
    return age.count + UNIT_COMPACT[age.unit];
  }

  // The screen-reader age: "12 s", "3 min", "5 h", "2 d" (shell.sb_sr).
  function ageWords(age) {
    return age.count + UNIT_WORDS[age.unit];
  }

  // One sidebar row (UI-03): the aria-hidden mono cell from its data-cell kind and data-since
  // instant, and the sr sentence after the name from the link's data-status and
  // data-delivery (06-UI-SPEC shell.sb_cell, shell.sb_sr).
  function renderSidebarRow(link, now) {
    var cell = link.querySelector('[data-live="sidebar-cell"]');
    var sentence = link.querySelector('[data-live="sidebar-sr"]');
    var age = cell ? isoAge(cell.getAttribute("data-since"), now) : null;
    if (cell) {
      var kind = cell.getAttribute("data-cell");
      if (kind === "age") {
        cell.textContent = age ? compactAge(age) : "\u2014";
      } else if (kind === "off") {
        cell.textContent = age ? "OFF " + compactAge(age) : "OFF";
      } else if (kind === "mnt") {
        cell.textContent = "MNT";
      } else if (kind === "wait") {
        cell.textContent = "\u2014";
      }
    }
    if (sentence) {
      var status = link.getAttribute("data-status");
      var text = null;
      if (status === "on") {
        text = age ? ", On, last heartbeat " + relativeText(age) : ", On";
      } else if (status === "off") {
        text = age ? ", Off for " + ageWords(age) : ", Off";
      } else if (status === "maintenance") {
        text = ", Maintenance";
      } else if (status === "waiting") {
        text = ", Waiting for first heartbeat";
      }
      if (text !== null) {
        if (link.getAttribute("data-delivery") === "failing") {
          text += ", delivery failing";
        }
        sentence.textContent = text;
      }
    }
  }

  // The theme choices; the server allowlist is context_processors.THEMES.
  var THEMES = ["light", "dark", "system"];

  // Live updates (UI-05): the status JSON's vocabulary. Only these values are ever written
  // into data-status and data-delivery; texts go through textContent.
  var STATUS_KEYS = ["on", "off", "maintenance", "waiting"];
  var DELIVERY_STATES = ["ok", "failing"];
  var NEVER = "Never";
  // The sidebar cell kind per status (data-cell).
  var CELL_KINDS = { on: "age", off: "off", maintenance: "mnt", waiting: "wait" };

  // The value of an object's own property, never one inherited from its prototype.
  function own(object, key) {
    return object && Object.prototype.hasOwnProperty.call(object, key) ? object[key] : undefined;
  }

  // A URL from the page (a data-* value or a link's href) that stays on this origin.
  function sameOrigin(url) {
    try {
      return new URL(url, window.location.href).origin === window.location.origin;
    } catch (error) {
      return false;
    }
  }

  // Shows the [data-delivery-variant] children of a live element that match the delivery
  // state (ok or failing) and hides the others: the delivery cell's OK text and failing
  // pill, the sidebar row's failing marks.
  function showDeliveryVariant(container, state) {
    container.querySelectorAll("[data-delivery-variant]").forEach(function (variant) {
      variant.hidden = variant.getAttribute("data-delivery-variant") !== state;
    });
  }

  // The innermost element of a time wrapper whose whole text is "Never" (no <time>).
  function neverHolder(wrapper) {
    if (wrapper.children.length === 0 && wrapper.textContent.trim() === NEVER) {
      return wrapper;
    }
    var elements = wrapper.querySelectorAll("*");
    for (var index = elements.length - 1; index >= 0; index -= 1) {
      var element = elements[index];
      if (element.children.length === 0 && element.textContent.trim() === NEVER) {
        return element;
      }
    }
    return null;
  }

  // Writes one instant of the status JSON ({iso, display, compact}, or null) into a time
  // wrapper rendered by partials/_time.html: the <time datetime>, its display parts (full,
  // or compact plus the tail) and the [data-relative] sibling. A wrapper rendered as
  // "Never" gets the display text in place of that word. Returns false when the wrapper
  // cannot show the value in place (the caller then offers a reload).
  function fillInstant(wrapper, instant) {
    var time = wrapper.querySelector("time");
    if (!instant) {
      if (time) {
        return false;
      }
      var filled = wrapper.querySelector("[data-instant-text]");
      if (filled) {
        filled.textContent = NEVER;
      }
      return true;
    }
    if (typeof instant.iso !== "string" || typeof instant.display !== "string") {
      return false;
    }
    if (!time) {
      var holder = wrapper.querySelector("[data-instant-text]") || neverHolder(wrapper);
      if (!holder) {
        return false;
      }
      holder.setAttribute("data-instant-text", "");
      holder.textContent = instant.display;
      return true;
    }
    var compact = typeof instant.compact === "string" ? instant.compact : "";
    var display = instant.display;
    time.setAttribute("datetime", instant.iso);
    var parts = wrapper.querySelectorAll("[data-part]");
    if (parts.length === 0) {
      time.textContent = display;
    }
    parts.forEach(function (part) {
      var name = part.getAttribute("data-part");
      if (name === "full") {
        part.textContent = display;
      } else if (name === "compact") {
        part.textContent = compact;
      } else if (name === "tail") {
        part.textContent = display.indexOf(compact) === 0 ? display.slice(compact.length) : "";
      }
    });
    var age = isoAge(instant.iso, serverNow());
    wrapper.querySelectorAll("[data-relative]").forEach(function (element) {
      element.setAttribute("data-relative", instant.iso);
      if (age) {
        element.textContent = relativeText(age);
      }
    });
    return true;
  }

  // Handlers components register for the page being shown again: the one window pageshow
  // listener at the end of this file runs them after the submit guard's reset.
  var pageshowHandlers = [];

  function onPageshow(handler) {
    pageshowHandlers.push(handler);
  }

  document.addEventListener("alpine:init", function () {
    // toasts (UI-09, UI-12): bound by x-data="toasts" on each toast region
    // (partials/_toasts.html). The server renders every toast; this component reveals the
    // Dismiss buttons, re-announces each toast's text, removes a success toast when its timer
    // bar ends (the bar pauses on hover and focus through CSS) and removes a dismissed toast.
    window.Alpine.data("toasts", function () {
      return {
        init: function () {
          var region = this.$el;
          region.querySelectorAll(TOAST + " " + DISMISS).forEach(function (button) {
            button.removeAttribute("hidden");
          });
          // Screen readers that ignore the content a live region had at load announce the
          // text when it is inserted again: clear it, then set it on the next frame.
          region.querySelectorAll('[data-testid="toast-text"]').forEach(function (element) {
            var value = element.textContent;
            element.textContent = "";
            window.requestAnimationFrame(function () {
              element.textContent = value;
            });
          });
          region.addEventListener("click", function (event) {
            var button = event.target instanceof Element ? event.target.closest(DISMISS) : null;
            var toast = button ? button.closest(TOAST) : null;
            if (toast && region.contains(toast)) {
              removeToast(toast);
            }
          });
          // animationend bubbles: only the timer bar's end removes its toast (toast-in, the
          // entry animation, ends on the toast itself).
          region.addEventListener("animationend", function (event) {
            var timer = event.target;
            if (timer instanceof Element && timer.hasAttribute("data-toast-timer")) {
              var toast = timer.closest(TOAST);
              if (toast) {
                removeToast(toast);
              }
            }
          });
        },
      };
    });

    // theme (UI-02, D6-03): bound by x-data="theme" and @submit="choose" on the theme form.
    // When the submitter's value is light, dark or system, choose cancels the POST, writes
    // the cookie with the attributes ThemeView sets (Path=/, Max-Age = THEME_MAX_AGE,
    // SameSite=Lax, Secure on https), sets <html data-theme> and the buttons' aria-pressed,
    // and never navigates. Without a usable submitter the native POST runs as without JS.
    window.Alpine.data("theme", function () {
      return {
        choose: function (event) {
          var form = event.target;
          var button = event.submitter;
          var value = button ? button.value : "";
          if (!(form instanceof HTMLFormElement) || THEMES.indexOf(value) < 0) {
            return;
          }
          // Cancelled here, on the form, before the event reaches the document's submit
          // guard, so the guard never marks this form or its buttons busy.
          event.preventDefault();
          document.cookie =
            "theme=" +
            value +
            "; Path=/; Max-Age=31536000; SameSite=Lax" +
            (window.location.protocol === "https:" ? "; Secure" : "");
          document.documentElement.setAttribute("data-theme", value);
          form.querySelectorAll('button[name="theme"]').forEach(function (option) {
            option.setAttribute("aria-pressed", option.value === value ? "true" : "false");
          });
        },
      };
    });

    // sidebar (UI-01, UI-12): bound by x-data="sidebar" on [data-testid="app-shell"].
    // Below lg the sidebar is a drawer: the hamburger opens it (<html data-drawer="open">,
    // which the CSS uses for the scroll lock and the slide-in), the body column and the skip
    // link become inert, Tab wraps inside the drawer, and Esc, the overlay and the close
    // button close it with the focus back on the hamburger; following a link closes it too.
    // On open the focus goes to the first nav link. Reaching lg closes it. At xl the rail
    // toggle collapses the sidebar (<html data-rail="collapsed">) and remembers the choice
    // under the one allowed key. toggleDrawer, closeDrawer and toggleRail can also be bound
    // by directives; the component binds its own hooks.
    window.Alpine.data("sidebar", function () {
      var root = document.documentElement;
      var aside = null;
      var toggle = null;
      var closer = null;
      var railButton = null;
      var overlay = null;
      var shellBody = null;
      var skipLink = null;
      var wide = null;
      var open = false;

      function setLabel(button, text) {
        var node = button ? labelNode(button) : null;
        if (node) {
          node.textContent = text;
        }
      }

      function setInert(element, inert) {
        if (!element) {
          return;
        }
        if (inert) {
          element.setAttribute("inert", "");
        } else {
          element.removeAttribute("inert");
        }
      }

      // Below lg a closed drawer is off-canvas: inert, so Tab never reaches its links.
      function syncAside() {
        setInert(aside, !open && !(wide && wide.matches));
      }

      function focusables() {
        var candidates = aside.querySelectorAll("a[href], button, input, select, textarea");
        return Array.prototype.filter.call(candidates, function (element) {
          return (
            !element.hidden &&
            !element.disabled &&
            element.getAttribute("tabindex") !== "-1" &&
            element.getClientRects().length > 0
          );
        });
      }

      function onKeydown(event) {
        if (event.key === "Escape") {
          event.preventDefault();
          setOpen(false, true);
          return;
        }
        if (event.key !== "Tab") {
          return;
        }
        var items = focusables();
        if (items.length === 0) {
          event.preventDefault();
          return;
        }
        var first = items[0];
        var last = items[items.length - 1];
        var active = document.activeElement;
        if (!aside.contains(active)) {
          event.preventDefault();
          (event.shiftKey ? last : first).focus();
        } else if (event.shiftKey && active === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && active === last) {
          event.preventDefault();
          first.focus();
        }
      }

      function setOpen(value, returnFocus) {
        if (!aside || value === open) {
          return;
        }
        open = value;
        if (open) {
          root.setAttribute("data-drawer", "open");
        } else {
          root.removeAttribute("data-drawer");
        }
        setInert(shellBody, open);
        setInert(skipLink, open);
        if (overlay) {
          overlay.hidden = !open;
        }
        if (toggle) {
          toggle.setAttribute("aria-expanded", open ? "true" : "false");
          setLabel(toggle, open ? "Close navigation" : "Open navigation");
        }
        syncAside();
        if (open) {
          document.addEventListener("keydown", onKeydown);
          var first = aside.querySelector("nav a[href]") || aside.querySelector("a[href]");
          if (first) {
            first.focus();
          }
        } else {
          document.removeEventListener("keydown", onKeydown);
          if (returnFocus && toggle) {
            toggle.focus();
          }
        }
      }

      function syncRail() {
        if (!railButton) {
          return;
        }
        var collapsed = root.getAttribute("data-rail") === "collapsed";
        var label = collapsed ? "Expand sidebar" : "Collapse sidebar";
        railButton.setAttribute("aria-expanded", collapsed ? "false" : "true");
        railButton.setAttribute("title", label);
        setLabel(railButton, label);
      }

      function saveRail(collapsed) {
        try {
          if (collapsed) {
            window.localStorage.setItem("powermon.sidebar.rail", "1");
          } else {
            window.localStorage.removeItem("powermon.sidebar.rail");
          }
        } catch (error) {
          // Storage is blocked: the choice lasts until the next page.
        }
      }

      function toggleDrawer(event) {
        if (firstCall(event)) {
          setOpen(!open, true);
        }
      }

      function closeDrawer(event) {
        if (firstCall(event)) {
          setOpen(false, true);
        }
      }

      function toggleRail(event) {
        if (!firstCall(event)) {
          return;
        }
        var collapsed = root.getAttribute("data-rail") !== "collapsed";
        if (collapsed) {
          root.setAttribute("data-rail", "collapsed");
        } else {
          root.removeAttribute("data-rail");
        }
        saveRail(collapsed);
        syncRail();
      }

      return {
        init: function () {
          var shell = this.$el;
          aside = document.getElementById("sidebar");
          toggle = shell.querySelector('[data-testid="sidebar-toggle"]');
          closer = shell.querySelector('[data-testid="drawer-close"]');
          railButton = shell.querySelector('[data-testid="rail-toggle"]');
          overlay = shell.querySelector("[data-drawer-overlay]");
          shellBody = shell.querySelector("[data-shell-body]");
          skipLink = document.querySelector('[data-testid="skip-link"]');
          wide = window.matchMedia("(min-width: 64rem)");
          if (aside) {
            reveal(toggle);
            reveal(closer);
            if (toggle) {
              toggle.addEventListener("click", toggleDrawer);
            }
            if (closer) {
              closer.addEventListener("click", closeDrawer);
            }
            if (overlay) {
              overlay.addEventListener("click", closeDrawer);
            }
            // Following a link closes the drawer; the navigation takes the focus.
            aside.addEventListener("click", function (event) {
              if (open && closestTo(event, "a[href]")) {
                setOpen(false, false);
              }
            });
            wide.addEventListener("change", function () {
              if (wide.matches) {
                setOpen(false, false);
              }
              syncAside();
            });
            syncAside();
          }
          if (railButton) {
            reveal(railButton);
            railButton.addEventListener("click", toggleRail);
            syncRail();
          }
        },
        toggleDrawer: toggleDrawer,
        closeDrawer: closeDrawer,
        toggleRail: toggleRail,
      };
    });

    // relative (UI-11): bound by x-data="relative" on the app layout's <body>, whose data-now
    // holds the server's instant at render. Every 15 s it rewrites each [data-relative] text
    // from its ISO value, each sidebar row's cell and sr sentence, and each [data-live-age]
    // (the LIVE indicator's "updated" age, from the last successful poll or the page render).
    // A pm:status event (a successful poll) resets that age.
    window.Alpine.data("relative", function () {
      var liveSince = null;

      function render() {
        var now = serverNow();
        document.querySelectorAll("[data-relative]").forEach(function (element) {
          var age = isoAge(element.getAttribute("data-relative"), now);
          if (age) {
            element.textContent = relativeText(age);
          }
        });
        document.querySelectorAll('a[data-testid="sidebar-location"]').forEach(function (link) {
          renderSidebarRow(link, now);
        });
        if (liveSince !== null) {
          var liveAge = relativeText(ageParts(liveSince, now));
          document.querySelectorAll("[data-live-age]").forEach(function (element) {
            element.textContent = liveAge;
          });
        }
      }

      return {
        init: function () {
          var rendered = syncClock(this.$el.getAttribute("data-now"));
          liveSince = rendered === null ? serverNow() : rendered;
          render();
          window.setInterval(render, RELATIVE_REFRESH_MS);
          window.addEventListener("pm:status", function (event) {
            var detail = event.detail || {};
            var generated = parseInstant(detail.generatedAt);
            liveSince = generated === null ? serverNow() : generated;
            render();
          });
        },
      };
    });

    // poll (UI-05): bound by x-data="poll" on main#main of the Locations list, the location
    // page and the setup page, with data-poll-url (the status JSON), data-poll-page (list,
    // detail or setup) and data-reload-url (the page's own URL). While the tab is visible it
    // GETs the status JSON every 30 s without following redirects, and at once when the tab
    // becomes visible again. After a failure (network, not 200, not the JSON) it waits 60,
    // 120, 240, then 300 s; after 3 failures in a row the LIVE indicator reads Paused and
    // the "Live updates paused" chip shows. An opaque redirect (the session has ended) stops
    // polling and shows the "Live updates paused" chip with the reload link; the sign-in page is
    // never read. A success writes attributes and text of the live elements only (unknown
    // ids are ignored), shows the "Status changed" reload chip when the page can no longer
    // match the data (list: another set of locations; location page: another status or
    // delivery state; any page: its location gone or a time it cannot show in place), and
    // dispatches pm:status on window.
    window.Alpine.data("poll", function () {
      var POLL_INTERVAL_MS = 30000;
      var POLL_BACKOFF_MS = [60000, 120000, 240000, 300000];
      var POLL_PAUSE_AFTER = 3;
      var POLL_TIMEOUT_MS = 15000;
      var main = null;
      var page = "";
      var timer = null;
      var busy = false;
      var stopped = false;
      var failures = 0;
      var changed = false;
      // Per location id, the status and delivery state the page was rendered with.
      var rendered = {};

      function snapshot() {
        main.querySelectorAll("[data-location-id]").forEach(function (element) {
          var id = element.getAttribute("data-location-id");
          var entry = own(rendered, id) || (rendered[id] = {});
          if (entry.status === undefined && element.hasAttribute("data-status")) {
            entry.status = element.getAttribute("data-status");
          }
          if (entry.delivery === undefined && element.hasAttribute("data-delivery")) {
            entry.delivery = element.getAttribute("data-delivery");
          }
        });
        // The location page's delivery row and banner carry data-delivery without an id.
        var ids = Object.keys(rendered);
        var delivery = main.querySelector("[data-delivery]");
        if (ids.length === 1 && rendered[ids[0]].delivery === undefined && delivery) {
          rendered[ids[0]].delivery = delivery.getAttribute("data-delivery");
        }
      }

      function showState() {
        var paused = stopped || failures >= POLL_PAUSE_AFTER;
        document.querySelectorAll('[data-testid="live-status"]').forEach(function (indicator) {
          indicator.setAttribute("data-live-state", paused ? "paused" : "live");
          var label = indicator.querySelector("[data-label]");
          if (label) {
            label.textContent = paused ? "Paused" : "Live";
          }
        });
        var chip = "updated";
        if (stopped) {
          chip = "paused-reload";
        } else if (failures >= POLL_PAUSE_AFTER) {
          chip = "paused";
        } else if (changed) {
          chip = "changed";
        }
        var chips = document.querySelectorAll('[data-testid="live-chip"] [data-chip]');
        chips.forEach(function (element) {
          element.hidden = element.getAttribute("data-chip") !== chip;
        });
      }

      function schedule(delay) {
        window.clearTimeout(timer);
        timer = null;
        if (!stopped && document.visibilityState === "visible") {
          timer = window.setTimeout(poll, delay);
        }
      }

      function updateLocation(element, entry) {
        var kind = element.getAttribute("data-live");
        var status = entry.status;
        if (kind === "status") {
          element.setAttribute("data-status", status);
          var label = element.querySelector("[data-label]");
          if (label && typeof entry.label === "string") {
            label.textContent = entry.label;
          }
        } else if (kind === "last-heartbeat") {
          if (!fillInstant(element, entry.last_heartbeat)) {
            changed = true;
          }
        } else if (kind === "since") {
          var since = entry.since;
          var sinceLabel = element.querySelector("[data-since-label]");
          if (since && sinceLabel) {
            sinceLabel.textContent = since.kind === "on" ? "On since" : "Outage since";
          }
          if (!fillInstant(element, since)) {
            changed = true;
          }
        } else if (kind === "delivery") {
          var state = entry.delivery.state;
          element.setAttribute("data-delivery", state);
          showDeliveryVariant(element, state);
          var pill =
            element.querySelector('[data-delivery-variant="failing"] [data-label]') ||
            element.querySelector("[data-label]");
          if (state === "failing" && pill && typeof entry.delivery.text === "string") {
            pill.textContent = entry.delivery.text;
          }
        } else if (kind === "first-heartbeat") {
          var received = status !== "waiting";
          var waitingLine = element.querySelector('[data-fh="waiting"]');
          var receivedLine = element.querySelector('[data-fh="received"]');
          if (waitingLine) {
            waitingLine.hidden = received;
          }
          if (receivedLine) {
            receivedLine.hidden = !received;
            var fillHere = !receivedLine.querySelector('[data-live="last-heartbeat"]');
            if (received && fillHere && !fillInstant(receivedLine, entry.last_heartbeat)) {
              changed = true;
            }
          }
        }
      }

      function updateSidebar(locations, now) {
        var rows = 'a[data-testid="sidebar-location"][data-location-id]';
        document.querySelectorAll(rows).forEach(function (link) {
          var entry = own(locations, link.getAttribute("data-location-id"));
          if (!entry) {
            return;
          }
          var status = entry.status;
          link.setAttribute("data-status", status);
          link.setAttribute("data-delivery", entry.delivery.state);
          showDeliveryVariant(link, entry.delivery.state);
          var cell = link.querySelector('[data-live="sidebar-cell"]');
          if (cell) {
            var since = "";
            if (status === "on" && entry.last_heartbeat) {
              since = entry.last_heartbeat.iso;
            } else if (status === "off" && entry.since && entry.since.kind === "outage") {
              since = entry.since.iso;
            }
            cell.setAttribute("data-cell", CELL_KINDS[status]);
            cell.setAttribute("data-since", typeof since === "string" ? since : "");
          }
          renderSidebarRow(link, now);
        });
      }

      function updateCounts(counts, total) {
        var number = function (value) {
          return typeof value === "number" && value >= 0 ? value : null;
        };
        var on = number(counts.on);
        var off = number(counts.off);
        var failing = number(counts.failing);
        if (on !== null && off !== null && failing !== null) {
          var dot = " \u00b7 ";
          var failingShort = failing > 0 ? dot + failing + " fail" : "";
          var failingWords = failing > 0 ? ", " + failing + " with delivery failing" : "";
          document.querySelectorAll('[data-live="summary"]').forEach(function (element) {
            element.textContent = on + " on" + dot + off + " off" + failingShort;
          });
          document.querySelectorAll('[data-live="summary-sr"]').forEach(function (element) {
            element.textContent = on + " on, " + off + " off" + failingWords;
          });
        }
        main.querySelectorAll('[data-live="count"]').forEach(function (element) {
          element.textContent = total === 1 ? "1 location" : total + " locations";
        });
        var setCount = function (element, value) {
          if (value === null) {
            return;
          }
          element.setAttribute("data-count", String(value));
          var shown = element.querySelector('[data-testid="fleet-count"]');
          if (shown) {
            shown.textContent = String(value);
          }
        };
        main.querySelectorAll('[data-testid="fleet-tile"][data-metric]').forEach(function (tile) {
          setCount(tile, number(own(counts, tile.getAttribute("data-metric"))));
        });
        main.querySelectorAll('[data-testid="fleet-total"]').forEach(function (cell) {
          setCount(cell, total);
        });
        main.querySelectorAll('[data-testid="fleet-bar"] [data-count]').forEach(function (segment) {
          var key = segment.getAttribute("data-status") || segment.getAttribute("data-metric");
          var value = number(own(counts, key));
          if (value !== null) {
            segment.setAttribute("data-count", String(value));
          }
        });
      }

      function apply(payload) {
        var locations = payload.locations;
        // Only entries in the payload's vocabulary are used; any other is an unknown id.
        var valid = {};
        Object.keys(locations).forEach(function (id) {
          var entry = locations[id];
          if (
            entry &&
            STATUS_KEYS.indexOf(entry.status) >= 0 &&
            entry.delivery &&
            DELIVERY_STATES.indexOf(entry.delivery.state) >= 0
          ) {
            valid[id] = entry;
          }
        });
        var ids = Object.keys(locations);
        var renderedIds = Object.keys(rendered);
        if (page === "list") {
          if (
            ids.length !== renderedIds.length ||
            renderedIds.some(function (id) {
              return own(locations, id) === undefined;
            })
          ) {
            changed = true;
          }
        }
        renderedIds.forEach(function (id) {
          var entry = own(valid, id);
          var before = rendered[id];
          if (!entry) {
            changed = true;
          } else if (
            page === "detail" &&
            (entry.status !== before.status ||
              (before.delivery !== undefined && entry.delivery.state !== before.delivery))
          ) {
            changed = true;
          }
        });
        var now = serverNow();
        document.querySelectorAll("[data-live][data-location-id]").forEach(function (element) {
          var entry = own(valid, element.getAttribute("data-location-id"));
          if (entry && element.getAttribute("data-live").indexOf("sidebar") !== 0) {
            updateLocation(element, entry);
          }
        });
        // The list's rows and phone cards carry the state the filter and the tone bars use.
        main
          .querySelectorAll('tr[data-testid="location-row"], a[data-testid="location-card"]')
          .forEach(function (element) {
            var entry = own(valid, element.getAttribute("data-location-id"));
            var holder = element.matches("tr") ? element : element.closest("li[data-status]");
            if (entry && holder) {
              holder.setAttribute("data-status", entry.status);
              holder.setAttribute("data-delivery", entry.delivery.state);
            }
          });
        updateSidebar(valid, now);
        updateCounts(payload.counts, ids.length);
      }

      function succeeded(payload) {
        failures = 0;
        syncClock(payload.generated_at);
        schedule(POLL_INTERVAL_MS);
        apply(payload);
        showState();
        var detail = { generatedAt: payload.generated_at, page: page };
        window.dispatchEvent(new CustomEvent("pm:status", { detail: detail }));
      }

      function failed() {
        failures += 1;
        schedule(POLL_BACKOFF_MS[Math.min(failures, POLL_BACKOFF_MS.length) - 1]);
        showState();
      }

      function ended() {
        stopped = true;
        schedule(0);
        showState();
      }

      function poll() {
        timer = null;
        if (stopped || busy || document.visibilityState !== "visible") {
          return;
        }
        busy = true;
        var controller = typeof AbortController === "function" ? new AbortController() : null;
        var timeout = controller
          ? window.setTimeout(function () {
              controller.abort();
            }, POLL_TIMEOUT_MS)
          : null;
        var finish = function () {
          busy = false;
          window.clearTimeout(timeout);
        };
        fetch(main.dataset.pollUrl, {
          redirect: "manual",
          cache: "no-store",
          signal: controller ? controller.signal : undefined,
        })
          .then(function (response) {
            if (response.type === "opaqueredirect") {
              return null;
            }
            if (response.status !== 200) {
              throw new Error("status " + response.status);
            }
            return response.json();
          })
          .then(
            function (payload) {
              finish();
              if (payload === null) {
                ended();
              } else if (
                payload &&
                typeof payload.locations === "object" &&
                payload.locations !== null &&
                typeof payload.counts === "object" &&
                payload.counts !== null
              ) {
                succeeded(payload);
              } else {
                failed();
              }
            },
            function () {
              finish();
              failed();
            }
          );
      }

      return {
        init: function () {
          main = this.$el;
          page = main.dataset.pollPage || "";
          if (!main.dataset.pollUrl || !sameOrigin(main.dataset.pollUrl)) {
            return;
          }
          snapshot();
          var reloadUrl = main.dataset.reloadUrl;
          var liveSlots = '[data-testid="live-status"], [data-testid="live-chip"]';
          document.querySelectorAll(liveSlots).forEach(reveal);
          if (reloadUrl && sameOrigin(reloadUrl)) {
            var reloadChips =
              '[data-testid="live-chip"] [data-chip="paused-reload"],' +
              ' [data-testid="live-chip"] [data-chip="changed"]';
            document.querySelectorAll(reloadChips).forEach(function (chip) {
              var link = chip.matches("a") ? chip : chip.querySelector("a");
              if (link) {
                link.setAttribute("href", reloadUrl);
              }
            });
          }
          // Step 5 of the setup page: the waiting line is JS only (it promises an update).
          document.querySelectorAll('[data-live="first-heartbeat"]').forEach(function (step) {
            var receivedLine = step.querySelector('[data-fh="received"]');
            if (!receivedLine || receivedLine.hidden) {
              reveal(step.querySelector('[data-fh="waiting"]'));
            }
          });
          showState();
          document.addEventListener("visibilitychange", function () {
            if (document.visibilityState === "visible") {
              window.clearTimeout(timer);
              poll();
            } else {
              window.clearTimeout(timer);
              timer = null;
            }
          });
          schedule(POLL_INTERVAL_MS);
        },
      };
    });

    // copy (UI-08): bound by x-data="copy" on each copy button, whose data-copy-target names
    // the element to copy and data-copied-msg the fixed message to announce. The button is
    // revealed only where the clipboard API exists. A click (and only a click) copies the
    // target's exact text, switches the visible label to "Copied" for 2 s (data-copied
    // shows the check) and announces the message in the page's polite [data-copy-status]
    // region; a rejected write keeps "Copy" and announces how to copy by hand. The value is
    // never kept in an attribute, and the clipboard is never read.
    window.Alpine.data("copy", function () {
      var COPIED_MS = 2000;
      var FAILED = "Copy failed. Select the text and copy it by hand.";

      function announce(text) {
        var region = document.querySelector("[data-copy-status]");
        if (!region) {
          return;
        }
        // Cleared first, so the same message twice in a row is announced twice.
        region.textContent = "";
        window.setTimeout(function () {
          region.textContent = text;
        }, 50);
      }

      return {
        init: function () {
          var button = this.$el;
          var target = document.getElementById(button.getAttribute("data-copy-target") || "");
          if (!target || !navigator.clipboard) {
            return;
          }
          var label = labelNode(button);
          var idle = label ? label.textContent : "";
          var timer = null;
          var reset = function () {
            window.clearTimeout(timer);
            if (label) {
              label.textContent = idle;
            }
            button.removeAttribute("data-copied");
          };
          reveal(button);
          button.addEventListener("click", function () {
            navigator.clipboard.writeText(target.textContent).then(
              function () {
                reset();
                if (label) {
                  label.textContent = "Copied";
                }
                button.setAttribute("data-copied", "");
                timer = window.setTimeout(reset, COPIED_MS);
                announce(button.getAttribute("data-copied-msg") || "Copied");
              },
              function () {
                reset();
                announce(FAILED);
              }
            );
          });
        },
      };
    });

    // tabs (UI-12): bound by x-data="tabs" on the setup page's examples step. It reveals the
    // tablist, shows the selected panel only (hidden on the others), and moves the selection
    // with a click or with Left, Right, Home and End on the focused tab (roving tabindex).
    // Without it the four panels stay stacked under their captions.
    window.Alpine.data("tabs", function () {
      return {
        init: function () {
          var list = this.$el.querySelector('[role="tablist"][data-testid="example-tabs"]');
          if (!list) {
            return;
          }
          var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
          var panels = tabs.map(function (tab) {
            return document.getElementById(tab.getAttribute("aria-controls") || "");
          });
          if (tabs.length === 0 || panels.indexOf(null) >= 0) {
            return;
          }
          var current = 0;
          tabs.forEach(function (tab, index) {
            if (tab.getAttribute("aria-selected") === "true") {
              current = index;
            }
          });
          var select = function (index, focus) {
            current = index;
            tabs.forEach(function (tab, position) {
              var selected = position === index;
              tab.setAttribute("aria-selected", selected ? "true" : "false");
              tab.setAttribute("tabindex", selected ? "0" : "-1");
              panels[position].hidden = !selected;
            });
            if (focus) {
              tabs[index].focus();
            }
          };
          tabs.forEach(function (tab, index) {
            tab.addEventListener("click", function () {
              select(index, false);
            });
          });
          list.addEventListener("keydown", function (event) {
            var next = null;
            if (event.key === "ArrowRight") {
              next = (current + 1) % tabs.length;
            } else if (event.key === "ArrowLeft") {
              next = (current - 1 + tabs.length) % tabs.length;
            } else if (event.key === "Home") {
              next = 0;
            } else if (event.key === "End") {
              next = tabs.length - 1;
            }
            if (next !== null) {
              event.preventDefault();
              select(next, true);
            }
          });
          select(current, false);
          reveal(list);
        },
      };
    });

    // revealGuard (R4): bound by x-data="revealGuard" on the revealed key region of the
    // setup page, whose data-masked-url is the setup URL (no key in it). When the page is
    // hidden (pagehide) it empties the key and the four examples, so a back/forward cache
    // copy holds no key; when such a copy is shown again (pageshow with persisted) it
    // replaces it with the masked setup page.
    window.Alpine.data("revealGuard", function () {
      var REVEALED_IDS = [
        "device-key",
        "example-curl",
        "example-cron",
        "example-wget-gnu",
        "example-wget-busybox",
      ];
      return {
        init: function () {
          var maskedUrl = this.$el.dataset.maskedUrl;
          window.addEventListener("pagehide", function () {
            REVEALED_IDS.forEach(function (id) {
              var element = document.getElementById(id);
              if (element) {
                element.textContent = "";
              }
            });
          });
          onPageshow(function (event) {
            if (event.persisted && maskedUrl && sameOrigin(maskedUrl)) {
              window.location.replace(maskedUrl);
            }
          });
        },
      };
    });

    // chartImage (UI-06): bound by x-data="chartImage" on the weekly chart figure. When the
    // image fails to load it reveals the [data-testid="weekly-chart-error"] warning and hides
    // the links to the image (around it and the card's "Open full size").
    window.Alpine.data("chartImage", function () {
      return {
        init: function () {
          var figure = this.$el;
          var card = figure.closest("section");
          var image = figure.querySelector("img");
          var warning =
            figure.querySelector('[data-testid="weekly-chart-error"]') ||
            (card ? card.querySelector('[data-testid="weekly-chart-error"]') : null);
          if (!image || !warning) {
            return;
          }
          var failed = function () {
            reveal(warning);
            var link = image.closest("a");
            if (link && figure.contains(link)) {
              link.hidden = true;
            }
            var fullSize = card ? card.querySelector('[data-testid="chart-full-size"]') : null;
            if (fullSize) {
              fullSize.hidden = true;
            }
          };
          image.addEventListener("error", failed);
          // The image may have failed before Alpine started.
          if (image.complete && image.naturalWidth === 0) {
            failed();
          }
        },
      };
    });

    // sectionNav (UI-12): bound by x-data="sectionNav" on the location page's section nav.
    // A click on one of its in-page links scrolls to the card (smoothly only when reduced
    // motion is not requested) and moves the focus to it (tabindex -1 when it has none).
    window.Alpine.data("sectionNav", function () {
      return {
        init: function () {
          var nav = this.$el;
          var reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
          nav.addEventListener("click", function (event) {
            var link = closestTo(event, 'a[href^="#"]');
            if (!link || !nav.contains(link) || event.button !== 0) {
              return;
            }
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
              return;
            }
            var id = link.getAttribute("href").slice(1);
            var target = id ? document.getElementById(id) : null;
            if (!target) {
              return;
            }
            event.preventDefault();
            if (!target.hasAttribute("tabindex")) {
              target.setAttribute("tabindex", "-1");
            }
            target.scrollIntoView({
              behavior: reduced.matches ? "auto" : "smooth",
              block: "start",
            });
            target.focus({ preventScroll: true });
          });
        },
      };
    });

    // confirmDialog (UI-07, D6-05): bound by x-data="confirmDialog" on the page's one
    // [data-confirm-scope] wrapper (location and setup pages), which only starts it. One
    // click listener on document handles a plain click on any same-origin a[data-confirm],
    // wherever it is (the kebab entries sit outside the wrapper); the dialog is the page's
    // one dialog[data-testid="confirm-dialog"] after main, found on document. A click opens
    // the dialog with its spinner and fetches the link's confirmation page as a fragment
    // (the fragment request header, no redirect followed). Only a 200 response that carries
    // the fragment header back and parses (DOMParser) to exactly one [data-testid="confirm"]
    // root is shown: the root is moved into the dialog body and Keep gets the focus. Any other
    // outcome closes the dialog and loads the confirmation page itself (location.assign),
    // which also shows a refusal's flash once. The dialog shell has only data-* hooks, so
    // its close button, Keep, the backdrop and Esc (cancel) are bound here; while the
    // destructive button is pending (the submit guard's aria-busy) they do nothing. On close
    // the focus goes back to the link, or to the menu button of the popover it was in.
    window.Alpine.data("confirmDialog", function () {
      return {
        init: function () {
          var dialog = document.querySelector('dialog[data-testid="confirm-dialog"]');
          if (!dialog || typeof dialog.showModal !== "function") {
            return;
          }
          var body = dialog.querySelector("[data-dialog-body]");
          var loading = dialog.querySelector("[data-dialog-loading]");
          var closeButton = dialog.querySelector("[data-dialog-close]");
          if (!body) {
            return;
          }
          var request = 0;
          var returnTo = null;
          var pressOnBackdrop = false;

          var pending = function () {
            var submit = body.querySelector('[data-testid="confirm-submit"]');
            return submit !== null && submit.getAttribute("aria-busy") === "true";
          };

          var close = function () {
            if (!pending() && dialog.open) {
              dialog.close();
            }
          };

          var outside = function (event) {
            var box = dialog.getBoundingClientRect();
            return (
              event.target === dialog &&
              (event.clientX < box.left ||
                event.clientX > box.right ||
                event.clientY < box.top ||
                event.clientY > box.bottom)
            );
          };

          // The element that gets the focus back: the link, or the button that opens the
          // popover menu the link sits in (the menu closes when the dialog opens).
          var focusTarget = function (link) {
            var popover = link.closest("[popover]");
            if (!popover || !popover.id) {
              return link;
            }
            try {
              if (popover.matches(":popover-open")) {
                popover.hidePopover();
              }
            } catch (error) {
              // No popover API: the menu is a plain list.
            }
            var openers = document.querySelectorAll("[popovertarget]");
            for (var index = 0; index < openers.length; index += 1) {
              if (openers[index].getAttribute("popovertarget") === popover.id) {
                return openers[index];
              }
            }
            return link;
          };

          var open = function (link) {
            var url = link.href;
            request += 1;
            var current = request;
            returnTo = focusTarget(link);
            body.textContent = "";
            reveal(loading);
            if (!dialog.open) {
              dialog.showModal();
            }
            fetch(link.href, { redirect: "manual", headers: { "X-PM-Fragment": "1" } })
              .then(function (response) {
                if (
                  response.type === "opaqueredirect" ||
                  response.status !== 200 ||
                  response.headers.get("X-PM-Fragment") !== "1"
                ) {
                  throw new Error("not a confirmation fragment");
                }
                return response.text();
              })
              .then(function (html) {
                if (current !== request) {
                  return;
                }
                var parsed = new DOMParser().parseFromString(html, "text/html");
                var roots = parsed.querySelectorAll('[data-testid="confirm"]');
                if (roots.length !== 1) {
                  throw new Error("not one confirmation root");
                }
                var root = roots[0];
                // Parsed scripts never run; they are dropped all the same.
                root.querySelectorAll("script").forEach(function (script) {
                  script.remove();
                });
                if (loading) {
                  loading.hidden = true;
                }
                body.appendChild(root);
                var keep = root.querySelector('[data-testid="keep"]');
                if (keep) {
                  keep.focus();
                }
              })
              .catch(function () {
                if (current !== request) {
                  return;
                }
                if (dialog.open) {
                  dialog.close();
                }
                window.location.assign(url);
              });
          };

          document.addEventListener("click", function (event) {
            if (event.defaultPrevented || event.button !== 0) {
              return;
            }
            if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
              return;
            }
            var link = closestTo(event, "a[data-confirm]");
            if (!link || dialog.contains(link) || !link.href || !sameOrigin(link.href)) {
              return;
            }
            if ((link.target && link.target !== "_self") || link.hasAttribute("download")) {
              return;
            }
            event.preventDefault();
            open(link);
          });

          if (closeButton) {
            closeButton.addEventListener("click", close);
          }
          dialog.addEventListener("click", function (event) {
            var keep = closestTo(event, '[data-testid="keep"]');
            if (keep && dialog.contains(keep)) {
              event.preventDefault();
              close();
            } else if (pressOnBackdrop && outside(event)) {
              close();
            }
          });
          dialog.addEventListener("pointerdown", function (event) {
            pressOnBackdrop = outside(event);
          });
          // Esc: cancel is prevented while pending; so is the keydown, for browsers whose
          // second Esc ignores the prevented cancel (06-RESEARCH Pitfall 10).
          dialog.addEventListener("cancel", function (event) {
            if (pending()) {
              event.preventDefault();
            }
          });
          dialog.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && pending()) {
              event.preventDefault();
            }
          });
          dialog.addEventListener("close", function () {
            // A load still running belongs to the closed dialog: it is ignored.
            request += 1;
            body.textContent = "";
            reveal(loading);
            if (returnTo && document.contains(returnTo)) {
              returnTo.focus();
            }
            returnTo = null;
          });
        },
      };
    });

    // fleetFilter (UI-04, N13): bound by x-data="fleetFilter" on the Locations page's
    // [data-fleet] wrapper (fleet card, table and phone cards). It reveals the filter cells
    // and the proportional bar (each segment's flex-grow is its data-count; empty segments
    // are hidden). Pressing a cell shows only the matching locations: a status cell by
    // data-status, Delivery failing by data-delivery; pressing the active cell or All shows
    // every location. Rows get data-filtered (the CSS collapses them, so columns never
    // move), cards get hidden, the description says how many are shown, and the no-match
    // line's "show all" resets the filter and focuses All. The filter is never in the URL;
    // it is applied again after each poll (pm:status).
    window.Alpine.data("fleetFilter", function () {
      var FILTERS = ["all", "on", "off", "maintenance", "waiting", "failing"];
      return {
        init: function () {
          var root = this.$el;
          var chips = Array.prototype.slice.call(
            root.querySelectorAll('button[data-testid="filter-chip"][data-filter]')
          );
          var bar = root.querySelector('[data-testid="fleet-bar"]');
          var showing = root.querySelector('[data-testid="fleet-showing"]');
          var active = "all";

          var matches = function (element) {
            if (active === "all") {
              return true;
            }
            if (active === "failing") {
              return element.getAttribute("data-delivery") === "failing";
            }
            return element.getAttribute("data-status") === active;
          };

          var sizeBar = function () {
            if (!bar) {
              return;
            }
            bar.querySelectorAll("[data-count]").forEach(function (segment) {
              var count = parseInt(segment.getAttribute("data-count"), 10);
              count = isNaN(count) || count < 0 ? 0 : count;
              segment.style.flexGrow = String(count);
              segment.hidden = count === 0;
            });
          };

          var describe = function (shown, total) {
            var noun = total === 1 ? " location" : " locations";
            if (active !== "all") {
              return "Showing " + shown + " of " + total + noun;
            }
            return total === 1 ? "Showing 1 location" : "Showing all " + total + noun;
          };

          var apply = function () {
            chips.forEach(function (chip) {
              var pressed = chip.getAttribute("data-filter") === active;
              chip.setAttribute("aria-pressed", pressed ? "true" : "false");
            });
            var rows = root.querySelectorAll('tr[data-testid="location-row"]');
            var cards = root.querySelectorAll("li[data-status][data-delivery]");
            var rowsShown = 0;
            var cardsShown = 0;
            rows.forEach(function (row) {
              if (matches(row)) {
                rowsShown += 1;
                row.removeAttribute("data-filtered");
              } else {
                row.setAttribute("data-filtered", "");
              }
            });
            cards.forEach(function (card) {
              var keep = matches(card);
              card.hidden = !keep;
              cardsShown += keep ? 1 : 0;
            });
            var shown = rows.length > 0 ? rowsShown : cardsShown;
            var counted = rows.length > 0 ? rows.length : cards.length;
            var declared = showing ? parseInt(showing.getAttribute("data-total"), 10) : NaN;
            var total = isNaN(declared) ? counted : declared;
            root.querySelectorAll('[data-testid="no-match"]').forEach(function (line) {
              line.hidden = shown !== 0;
            });
            if (showing) {
              showing.textContent = describe(shown, total);
            }
          };

          var press = function (filter) {
            if (FILTERS.indexOf(filter) < 0) {
              return;
            }
            active = filter === active || filter === "all" ? "all" : filter;
            apply();
          };

          chips.forEach(reveal);
          reveal(bar);
          root.addEventListener("click", function (event) {
            var chip = closestTo(event, 'button[data-testid="filter-chip"][data-filter]');
            var reset = closestTo(event, "[data-filter-reset]");
            if (chip && root.contains(chip)) {
              press(chip.getAttribute("data-filter"));
            } else if (reset && root.contains(reset)) {
              event.preventDefault();
              active = "all";
              apply();
              var all = root.querySelector('button[data-testid="filter-chip"][data-filter="all"]');
              if (all) {
                all.focus();
              }
            }
          });
          window.addEventListener("pm:status", function () {
            sizeBar();
            apply();
          });
          sizeBar();
          apply();
        },
      };
    });

    // offAfterHint (N8): bound by x-data="offAfterHint" on the location form's monitoring
    // section. While both seconds fields (#id_period_s, #id_grace_s) hold whole numbers from
    // 10 to 3600 (the form's own bounds), the hint shows their sum in [data-off-after-value];
    // otherwise it shows the [data-off-after-fallback] sentence. The server stays the
    // source of truth: the form validates the values again.
    window.Alpine.data("offAfterHint", function () {
      var OFF_AFTER_MIN = 10;
      var OFF_AFTER_MAX = 3600;

      var seconds = function (input) {
        var text = input.value.trim();
        if (!/^\d+$/.test(text)) {
          return null;
        }
        var value = parseInt(text, 10);
        return value >= OFF_AFTER_MIN && value <= OFF_AFTER_MAX ? value : null;
      };

      // Writes the number into the value element: in place of the first number in its text,
      // or as its whole text when it is a bare slot. False when there is no place for it.
      var writeNumber = function (element, number) {
        var walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT);
        for (var node = walker.nextNode(); node; node = walker.nextNode()) {
          if (/\d/.test(node.textContent)) {
            node.textContent = node.textContent.replace(/\d+/, String(number));
            return true;
          }
        }
        if (element.children.length === 0) {
          element.textContent = String(number);
          return true;
        }
        return false;
      };

      return {
        init: function () {
          var section = this.$el;
          var period = document.getElementById("id_period_s");
          var grace = document.getElementById("id_grace_s");
          var value = section.querySelector("[data-off-after-value]");
          var fallback = section.querySelector("[data-off-after-fallback]");
          if (!period || !grace || !value || !fallback) {
            return;
          }
          // The sentence shown instead of the fallback: the value element, or its ancestor
          // that sits next to the fallback.
          var sentence = value;
          while (sentence.parentElement && sentence.parentElement !== fallback.parentElement) {
            if (sentence.parentElement === section) {
              sentence = value;
              break;
            }
            sentence = sentence.parentElement;
          }
          var update = function () {
            var periodSeconds = seconds(period);
            var graceSeconds = seconds(grace);
            var valid =
              periodSeconds !== null &&
              graceSeconds !== null &&
              writeNumber(value, periodSeconds + graceSeconds);
            sentence.hidden = !valid;
            fallback.hidden = valid;
          };
          period.addEventListener("input", update);
          grace.addEventListener("input", update);
          update();
        },
      };
    });

    // errorSummary (N10, UI-12): bound by x-data="errorSummary" on the form's error summary
    // ([tabindex="-1"]): it takes the focus on load, so its jump links are read first.
    window.Alpine.data("errorSummary", function () {
      return {
        init: function () {
          this.$el.focus();
        },
      };
    });

    // throttleCountdown (N11): bound by x-data="throttleCountdown" on the sign-in card in
    // the throttled state. It counts down from the throttle message's data-retry-after
    // seconds in the [data-testid="throttle-countdown"] slot, "({m:ss} left)", with the
    // sign-in button aria-disabled (the submit guard refuses an aria-disabled submitter),
    // then reads "(you can try again now)" and removes aria-disabled. The button is never
    // disabled; without JS a POST simply gets the 429 again.
    window.Alpine.data("throttleCountdown", function () {
      return {
        init: function () {
          var root = this.$el;
          var message = root.querySelector("[data-retry-after]");
          var output = root.querySelector('[data-testid="throttle-countdown"]');
          var button = root.querySelector('button[type="submit"]');
          var total = message ? parseInt(message.getAttribute("data-retry-after"), 10) : NaN;
          if (!output || !button || isNaN(total) || total <= 0) {
            return;
          }
          var deadline = window.performance.now() + total * MS_PER_SECOND;
          var timer = null;
          var tick = function () {
            var left = Math.ceil((deadline - window.performance.now()) / MS_PER_SECOND);
            if (left <= 0) {
              window.clearInterval(timer);
              output.textContent = "(you can try again now)";
              button.removeAttribute("aria-disabled");
              return;
            }
            var rest = left % 60;
            var clock = Math.floor(left / 60) + ":" + (rest < 10 ? "0" : "") + rest;
            output.textContent = "(" + clock + " left)";
          };
          button.setAttribute("aria-disabled", "true");
          reveal(output);
          tick();
          timer = window.setInterval(tick, MS_PER_SECOND);
        },
      };
    });
  });

  // The submit guard (UI-09): one delegated listener for every POST form. The first submit
  // of a form marks it (data-submitted) and its submitter (aria-busy and aria-disabled, the
  // visible label swapped for data-pending-label when the button has one); a later submit of
  // the same form is prevented. It never sets the disabled attribute, which would drop the
  // clicked button's name and value from the POST, and it never submits anything itself. A
  // submit another handler already prevented (the theme switch) is left alone, and a
  // submitter marked aria-disabled by a component does not submit. The pageshow listener
  // below clears every mark when the browser restores the page from its back/forward cache.
  var pending = [];

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement) || form.method !== "post") {
      return;
    }
    var button = event.submitter || null;
    if (form.hasAttribute("data-submitted") || (button && button.getAttribute("aria-disabled") === "true")) {
      event.preventDefault();
      return;
    }
    form.setAttribute("data-submitted", "");
    var entry = { form: form, button: button, ariaBusy: null, ariaDisabled: null, label: null, text: "" };
    if (button) {
      entry.ariaBusy = button.getAttribute("aria-busy");
      entry.ariaDisabled = button.getAttribute("aria-disabled");
      button.setAttribute("aria-busy", "true");
      button.setAttribute("aria-disabled", "true");
      var pendingLabel = button.getAttribute("data-pending-label");
      var label = pendingLabel ? labelNode(button) : null;
      if (label) {
        entry.label = label;
        entry.text = label.textContent;
        label.textContent = pendingLabel;
      }
    }
    pending.push(entry);
  });

  // Restores an attribute to the value it had before the guard set it (null: absent).
  function restore(element, name, value) {
    if (value === null) {
      element.removeAttribute(name);
    } else {
      element.setAttribute(name, value);
    }
  }

  window.addEventListener("pageshow", function (event) {
    pending.forEach(function (entry) {
      entry.form.removeAttribute("data-submitted");
      if (entry.button) {
        restore(entry.button, "aria-busy", entry.ariaBusy);
        restore(entry.button, "aria-disabled", entry.ariaDisabled);
      }
      if (entry.label) {
        entry.label.textContent = entry.text;
      }
    });
    pending = [];
    pageshowHandlers.forEach(function (handler) {
      handler(event);
    });
  });
})();
