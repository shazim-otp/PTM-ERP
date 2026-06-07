function toggleTheme() {
    let body = document.body;

    if (body.classList.contains("dark")) {
        body.classList.remove("dark");
        body.classList.add("light");
        localStorage.setItem("theme", "light");
    } else {
        body.classList.remove("light");
        body.classList.add("dark");
        localStorage.setItem("theme", "dark");
    }
}

// Load saved theme
window.onload = () => {
    let saved = localStorage.getItem("theme") || "dark";
    document.body.classList.add(saved);
};