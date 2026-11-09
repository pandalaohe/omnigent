"use strict";
function render(data) {
  if (!data) return;
  document.getElementById("rows").replaceChildren(
    ...data.rows.map((row, index) => {
      const button = document.createElement("button");
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(index === data.index));
      const icon = document.createElement("span");
      icon.textContent = row.bookmarked ? "★" : "↗";
      const label = document.createElement("span");
      const title = document.createElement("strong");
      title.textContent = row.title;
      const url = document.createElement("small");
      url.textContent = row.url;
      label.append(title, url);
      button.append(icon, label);
      button.onclick = () => window.places.choose(index);
      return button;
    }),
  );
}
window.places.onData(render);
void window.places.data().then(render);
