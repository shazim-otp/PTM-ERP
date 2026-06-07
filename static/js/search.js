function escapeHTML(str) {
    if (!str) return '';
    return str.replace(/[&<>'"]/g, 
        tag => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[tag] || tag)
    );
}

async function searchStudent() {
    let searchInput = document.getElementById("search");
    let resultsDiv = document.getElementById("results");

    if (!searchInput || !resultsDiv) {
        return;
    }

    let query = searchInput.value;

    if (query.length === 0) {
        resultsDiv.innerHTML = "";
        resultsDiv.classList.add("d-none");
        return;
    }

    let url = `/search?q=${encodeURIComponent(query)}`;
    
    let typeSelect = document.getElementById("type");
    if (typeSelect && typeSelect.value) {
        url += `&type=${encodeURIComponent(typeSelect.value)}`;
    }
    
    let streamSelect = document.getElementById("stream");
    if (streamSelect && streamSelect.value) {
        url += `&stream=${encodeURIComponent(streamSelect.value)}`;
    }
    
    let categorySelect = document.getElementById("category");
    if (categorySelect && categorySelect.value) {
        url += `&category=${encodeURIComponent(categorySelect.value)}`;
    }

    try {
        let res = await fetch(url);
        let data = await res.json();

        let html = "";

        if (data.length === 0) {
            html = "<div class='result-item text-white'>No results found</div>";
        } else {
            data.forEach(item => {
                let icon = item.type === "teacher" ? "👨‍🏫" : "🎓";
                let escapedName = escapeHTML(item.name);
                html += `<div class="result-item text-white" onclick="openItem('${item.type}', ${item.id})">
                            ${icon} ${escapedName} <span class="badge bg-secondary ms-2">${item.type}</span>
                         </div>`;
            });
        }

        resultsDiv.innerHTML = html;
        resultsDiv.classList.remove("d-none");
    } catch (e) {
        console.error("Search failed:", e);
    }
}

function openItem(type, id) {
    if (type === "teacher") {
        window.location.href = `/teacher/${id}`;
    } else {
        window.location.href = `/student/${id}`;
    }
}
