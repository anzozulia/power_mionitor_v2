/*
 * Power Monitor admin script: the one first-party script, a classic script loaded in <head>
 * without defer. Its first statement applies the stored sidebar rail flag to <html> before
 * first paint; its top level touches nothing else on the page.
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
})();
