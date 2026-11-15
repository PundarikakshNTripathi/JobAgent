"""Logos for the FICTIONAL demo companies (drawn here, not copied from anyone), written into a demo workspace's logo cache so the GUI shows
real-looking marks in screenshots and GIFs without fetching anything. Real companies in a real workspace get their real logos as usual."""
import os

S = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{}</svg>'
LOGOS = {
    "Northwind AI": '<rect width="64" height="64" rx="14" fill="#1d3557"/><path d="M16 44 44 16M28 16h16v16" stroke="#fff" stroke-width="6" fill="none" stroke-linecap="round" stroke-linejoin="round"/>',
    "Contoso Labs": '<rect width="64" height="64" rx="14" fill="#e76f51"/><path d="M26 14h12M29 14v14L18 48a4 4 0 0 0 3.5 6h21a4 4 0 0 0 3.5-6L35 28V14" stroke="#fff" stroke-width="4.5" fill="none" stroke-linejoin="round"/>',
    "Fabrikam Robotics": '<path d="M32 4 56 18v28L32 60 8 46V18z" fill="#6d28d9"/><circle cx="32" cy="32" r="9" fill="#fff"/><circle cx="32" cy="32" r="4" fill="#6d28d9"/>',
    "Tailspin Systems": '<circle cx="32" cy="32" r="30" fill="#0f766e"/><path d="M32 18a14 14 0 1 1-14 14 9 9 0 0 1 18 0 4 4 0 0 1-8 0" stroke="#fff" stroke-width="5" fill="none" stroke-linecap="round"/>',
    "Adatum Research": '<rect width="64" height="64" rx="14" fill="#1f2937"/><g fill="none" stroke="#fbbf24" stroke-width="3.5"><ellipse cx="32" cy="32" rx="22" ry="8"/><ellipse cx="32" cy="32" rx="22" ry="8" transform="rotate(60 32 32)"/><ellipse cx="32" cy="32" rx="22" ry="8" transform="rotate(-60 32 32)"/></g><circle cx="32" cy="32" r="4" fill="#fbbf24"/>',
    "Wingtip Compute": '<rect width="64" height="64" rx="14" fill="#0284c7"/><path d="M10 40 54 18 34 46l-6-8z" fill="#fff"/>',
    "Litware Data": '<rect width="64" height="64" rx="14" fill="#15803d"/><rect x="14" y="34" width="9" height="16" rx="2" fill="#fff"/><rect x="27.5" y="24" width="9" height="26" rx="2" fill="#fff"/><rect x="41" y="14" width="9" height="36" rx="2" fill="#fff"/>',
    "Proseware": '<rect width="64" height="64" rx="32" fill="#be123c"/><path d="M24 48V16h10a10 10 0 0 1 0 20H24" stroke="#fff" stroke-width="6" fill="none" stroke-linejoin="round"/>',
    "Woodgrove Analytics": '<rect width="64" height="64" rx="14" fill="#365314"/><path d="M32 10 50 40H14z" fill="#a3e635"/><path d="M32 22 46 46H18z" fill="#bef264"/><rect x="29" y="44" width="6" height="10" fill="#a3e635"/>',
    "Alpine Kernels": '<rect width="64" height="64" rx="14" fill="#e0f2fe"/><path d="M6 50 24 22l10 14 6-8 18 22z" fill="#1e40af"/><path d="M24 22l5 7.5-5-2-4 3z" fill="#fff"/>',
}


def write(cache_dir, slug):
    os.makedirs(cache_dir, exist_ok=True)
    for name, body in LOGOS.items():
        with open(os.path.join(cache_dir, slug(name) + ".img"), "w", encoding="utf-8") as f:
            f.write(S.format(body))
    return len(LOGOS)
