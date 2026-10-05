/*
 * Power Monitor admin script: the one first-party script, a classic script loaded in <head>
 * without defer. Its first statement applies the stored sidebar rail flag to <html> before
 * first paint. The rest of its top level only registers listeners and touches nothing on the
 * page: alpine:init (the Alpine.data components, bound by x-data in the templates), the
 * delegated submit guard on document and the pageshow reset.
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
              var link = event.target instanceof Element ? event.target.closest("a[href]") : null;
              if (open && link) {
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

  window.addEventListener("pageshow", function () {
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
  });
})();
