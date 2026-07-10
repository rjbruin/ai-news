// Vanilla-JS drag-and-drop three-tier topic picker (Complete / Highlights /
// None). Complete is the implicit default tier and is never submitted —
// only highlights/none badges get mirrored into hidden inputs, named
// `<field>_highlights` / `<field>_none`, read server-side with
// request.form.getlist(). Click/tap cycles a badge through the tiers, for
// touch devices where HTML5 drag-and-drop isn't available.
(function () {
  const TIER_ORDER = ["complete", "highlights", "none"];
  const BADGE_CLASS = {
    complete: "badge text-bg-primary tier-badge",
    highlights: "badge text-bg-secondary tier-badge",
    none: "badge text-bg-light text-dark border tier-badge",
  };

  function syncHiddenInputs(root) {
    const fieldName = root.dataset.fieldName;
    const wrap = root.querySelector(".tier-picker-hidden-inputs");
    wrap.innerHTML = "";
    root.querySelectorAll(".tier-box").forEach((box) => {
      const tier = box.dataset.tier;
      if (tier === "complete") return; // implicit remainder, not submitted
      box.querySelectorAll(".tier-badge").forEach((badge) => {
        const input = document.createElement("input");
        input.type = "hidden";
        input.name = fieldName + "_" + tier;
        input.value = badge.dataset.tagId;
        wrap.appendChild(input);
      });
    });
  }

  function moveBadge(root, badge, tier) {
    const box = root.querySelector('.tier-box[data-tier="' + tier + '"]');
    if (!box) return;
    badge.className = BADGE_CLASS[tier] + (badge.className.includes("dragging") ? " dragging" : "");
    box.querySelector(".tier-box-badges").appendChild(badge);
    syncHiddenInputs(root);
  }

  function initTierPicker(root) {
    root.querySelectorAll(".tier-badge").forEach((badge) => {
      badge.addEventListener("dragstart", (e) => {
        e.dataTransfer.setData("text/plain", badge.dataset.tagId);
        e.dataTransfer.effectAllowed = "move";
        badge.classList.add("dragging");
      });
      badge.addEventListener("dragend", () => badge.classList.remove("dragging"));
      badge.addEventListener("click", () => {
        const box = badge.closest(".tier-box");
        const current = box ? box.dataset.tier : "complete";
        const next = TIER_ORDER[(TIER_ORDER.indexOf(current) + 1) % TIER_ORDER.length];
        moveBadge(root, badge, next);
      });
      badge.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          badge.click();
        }
      });
    });

    root.querySelectorAll(".tier-box").forEach((box) => {
      box.addEventListener("dragover", (e) => {
        e.preventDefault();
        box.classList.add("drag-over");
      });
      box.addEventListener("dragleave", () => box.classList.remove("drag-over"));
      box.addEventListener("drop", (e) => {
        e.preventDefault();
        box.classList.remove("drag-over");
        const tagId = e.dataTransfer.getData("text/plain");
        const badge = root.querySelector('.tier-badge[data-tag-id="' + tagId + '"]');
        if (badge) moveBadge(root, badge, box.dataset.tier);
      });
    });

    syncHiddenInputs(root);
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".tier-picker").forEach(initTierPicker);
  });
})();
