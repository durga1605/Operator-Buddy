document.addEventListener("DOMContentLoaded", function () {
    const searchInput = document.getElementById("plantSearch");
    const dropdown = document.getElementById("plantDropdown");
    const options = document.querySelectorAll(".plant-option");
    const hiddenInput = document.getElementById("plant_code");
    if (!searchInput || !dropdown || !hiddenInput) return;

    searchInput.addEventListener("focus", () => {
        dropdown.classList.remove("d-none");
    });
    searchInput.addEventListener("keyup", () => {
        const val = searchInput.value.toLowerCase();
        options.forEach(opt => {
            opt.style.display = opt.innerText.toLowerCase().includes(val) ? "block" : "none";
        });
    });
    options.forEach(opt => {
        opt.addEventListener("click", () => {
            searchInput.value = opt.innerText;
            hiddenInput.value = opt.dataset.value;
            dropdown.classList.add("d-none");
        });
    });
    document.addEventListener("click", (e) => {
        if (!e.target.closest(".plant-select")) {
            dropdown.classList.add("d-none");
        }
    });
});
