// Vanilla-JS autocomplete + removable-badges picker for Topics.
// No server round-trip — the full candidate list is small (tens of topics)
// and embedded as a data attribute, so filtering is a client-side substring
// match. Multiple independent instances on one page are supported.
(function () {
  function selectedIds(root) {
    return new Set(
      Array.from(root.querySelectorAll(".topic-picker-hidden-inputs input")).map(
        (el) => el.value
      )
    );
  }

  function addSelection(root, fieldName, id, name) {
    if (selectedIds(root).has(String(id))) return;

    const badges = root.querySelector(".topic-picker-badges");
    const badge = document.createElement("span");
    badge.className =
      "badge text-bg-secondary topic-picker-badge d-inline-flex align-items-center gap-1";
    badge.dataset.tagId = String(id);
    badge.textContent = name + " ";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn-close btn-close-white";
    closeBtn.style.fontSize = ".55rem";
    closeBtn.setAttribute("aria-label", "Remove");
    badge.appendChild(closeBtn);
    badges.appendChild(badge);

    const hiddenInputs = root.querySelector(".topic-picker-hidden-inputs");
    const input = document.createElement("input");
    input.type = "hidden";
    input.name = fieldName;
    input.value = String(id);
    hiddenInputs.appendChild(input);
  }

  function removeSelection(root, id) {
    root
      .querySelectorAll(`.topic-picker-badge[data-tag-id="${id}"]`)
      .forEach((el) => el.remove());
    root
      .querySelectorAll(`.topic-picker-hidden-inputs input[value="${id}"]`)
      .forEach((el) => el.remove());
  }

  function initTopicPicker(root) {
    const fieldName = root.dataset.fieldName;
    let allTopics = [];
    try {
      allTopics = JSON.parse(root.dataset.allTopics || "[]");
    } catch (e) {
      allTopics = [];
    }

    const search = root.querySelector(".topic-picker-search");
    const dropdown = root.querySelector(".topic-picker-dropdown");

    function renderDropdown(query) {
      const q = query.trim().toLowerCase();
      const taken = selectedIds(root);
      const matches = allTopics
        .filter((t) => !taken.has(String(t.id)))
        .filter((t) => !q || t.name.toLowerCase().includes(q))
        .slice(0, 8);

      dropdown.innerHTML = "";
      if (!matches.length) {
        dropdown.style.display = "none";
        return;
      }
      matches.forEach((t) => {
        const item = document.createElement("button");
        item.type = "button";
        item.className = "list-group-item list-group-item-action py-1 small";
        item.textContent = t.name;
        item.addEventListener("click", () => {
          addSelection(root, fieldName, t.id, t.name);
          search.value = "";
          dropdown.style.display = "none";
        });
        dropdown.appendChild(item);
      });
      dropdown.style.display = "block";
    }

    search.addEventListener("input", () => renderDropdown(search.value));
    search.addEventListener("focus", () => renderDropdown(search.value));
    document.addEventListener("click", (e) => {
      if (!root.contains(e.target)) dropdown.style.display = "none";
    });

    root.querySelector(".topic-picker-badges").addEventListener("click", (e) => {
      const closeBtn = e.target.closest(".btn-close");
      if (!closeBtn) return;
      const badge = closeBtn.closest(".topic-picker-badge");
      if (badge) removeSelection(root, badge.dataset.tagId);
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".topic-picker").forEach(initTopicPicker);
  });
})();
