/*
 * Power Monitor admin script: the one first-party script, a classic script loaded in <head>
 * without defer. Its first statement applies the stored sidebar rail flag to <html> before
 * first paint. The rest of its top level only registers listeners and touches nothing on the
 * page: alpine:init (the Alpine.data components, bound by x-data in the templates), the
 * delegated submit guard on document and the pageshow reset.
 *
 * Rules (06-UI-SPEC Interaction Contract, R1, R4): every DOM write sets an attribute or
 * textContent; no browser storage beyond the rail flag; every POST stays a native form
 * submission. The page works without this script; it only enhances it.
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

  // The first text node of the button's visible label (the icon's markup holds no text).
  function labelNode(button) {
    var walker = document.createTreeWalker(button, NodeFilter.SHOW_TEXT);
    for (var node = walker.nextNode(); node; node = walker.nextNode()) {
      if (node.textContent.trim() !== "") {
        return node;
      }
    }
    return null;
  }

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
