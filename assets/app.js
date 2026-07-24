"use strict";

const dateFormatter = new Intl.DateTimeFormat("da-DK", {
  dateStyle: "medium",
  timeZone: "Europe/Copenhagen",
});
const dateTimeFormatter = new Intl.DateTimeFormat("da-DK", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "Europe/Copenhagen",
});

function validDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function renderArticle(article) {
  const item = document.createElement("li");
  item.className = "article";

  const heading = document.createElement("h2");
  const link = document.createElement("a");
  link.href = article.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = article.headline;
  heading.append(link);

  const meta = document.createElement("p");
  meta.className = "meta";
  const published = validDate(article.publication_date);
  meta.textContent = `${article.publisher || "Ukendt udgiver"} · ${
    published ? dateFormatter.format(published) : "Ukendt dato"
  }`;

  item.append(heading, meta);
  return item;
}

async function loadArticles() {
  const status = document.querySelector("#status");
  try {
    const response = await fetch("data/articles.json", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const articles = Array.isArray(data.articles) ? [...data.articles] : [];
    articles.sort(
      (a, b) =>
        (validDate(b.publication_date)?.getTime() || 0) -
        (validDate(a.publication_date)?.getTime() || 0),
    );

    document.querySelector("#total").textContent = articles.length.toLocaleString("da-DK");
    const updated = validDate(data.last_successful_update);
    const updatedElement = document.querySelector("#updated");
    updatedElement.textContent = updated ? dateTimeFormatter.format(updated) : "aldrig";
    if (updated) updatedElement.dateTime = updated.toISOString();

    const list = document.querySelector("#articles");
    list.replaceChildren(...articles.map(renderArticle));
    status.textContent = articles.length ? "" : "Der er endnu ikke fundet nogen omtaler.";
    status.hidden = articles.length > 0;
  } catch (error) {
    console.error("Could not load article data", error);
    status.textContent = "Omtalerne kunne ikke indlæses. Prøv igen senere.";
    status.classList.add("error");
  }
}

loadArticles();
