# PartHunter — installable app

PartHunter is an installable web app that searches a part number (or keywords like "ryco oil filter") across live Australian parts stores and, with an optional search key, the wider web via Google Shopping, then compares the returned listings. The local server makes these requests when you search; the app shell and saved starter examples work offline after the app has loaded once, but live searches and retailer pages need an internet connection.

## Start the app

1. Unzip the download.
2. On macOS, double-click **Start PartHunter.command**. On Windows, double-click **Start PartHunter.bat**. Python 3 must be installed.
3. The launcher opens PartHunter in your browser at `http://localhost:8765`. Keep the launcher window open while using it.
4. Choose **Install app** in PartHunter if your browser shows that button. If it does not, use your browser's install option (Chrome/Edge menu → **Install PartHunter**). On macOS Safari, use **File → Add to Dock**. On iPhone/iPad, first host the folder over HTTPS, then use Safari's **Share → Add to Home Screen**.

The local server normally uses port 8765 and remembers its chosen port between launches. If an older PartHunter server is still using 8765, the new launcher opens on the next available port and remembers it. If every port from 8765 to 8785 is busy, close an older PartHunter server and try again.

## Included data

Every search sends the entered part number to the three participating retailers. Exact product-code matches are listed separately from possible cross-references. Live catalogue prices and availability are read at search time, but delivery and local checkout stock can differ. Fitment still needs confirmation with the seller. If the participating catalogues have no match, the app links to direct retailer, Google Shopping, and eBay Australia searches.

The saved examples currently contain Ryco Z436 and Bosch A298S. Their saved prices are used only if live search is unavailable and are marked as saved fallback data.

This is an installable web app (PWA), not an App Store or Google Play native package.

## Searching more of the web

Open **Search sources** under the search box:

- **Stores** – tick or untick A1, Auto One and Sunbury Auto Parts, or add any other Shopify-based store by its address.
- **Web search key** – paste a key from [SearchAPI.io](https://www.searchapi.io) or [SerpApi](https://serpapi.com). PartHunter then also pulls Google Shopping (Australia) results, which covers sellers such as Supercheap Auto, Repco, Autobarn, eBay and Amazon wherever Google lists them. Without a key, those sites are offered as one-click search links instead. You can also set `PARTHUNTER_SEARCHAPI_KEY` or `PARTHUNTER_SERPAPI_KEY` as environment variables before launching.

Keys and settings are stored in your browser and only sent to your local PartHunter server and the search service. Results can be sorted, filtered to hide unavailable items, or limited to exact matches. If you enter your car, listings that mention its make and model are flagged; that is a hint, not a fitment check.

**Open-web search (default, no key):** PartHunter finds product pages on any site through DuckDuckGo and Bing, opens each one, and reads its price from the page's product data. This is best effort: search engines can block automated lookups, and sites that hide prices in scripts won't return one. Untick it under Search sources to turn it off.

## Searching by part name

Use the **Part name** bar (e.g. "oil filter", "brake pads") to compare every brand PartHunter can find. Results are grouped by brand, with the cheapest listing overall highlighted, plus the cheapest per brand. Name searches check your selected stores (and a web search key if set) and take a little longer, because each store is asked about the name and about each common brand.

Filters above the results work for both search types: price range (min/max), Budget / Mid-range / Premium (the cheapest, middle and top third of the prices found), brand tick-boxes, hide unavailable, and sort by price, brand or retailer.

## Stores searched directly

PartHunter now reads 7 stores on every search: A1 Autoparts Niddrie, Auto One and Sunbury Auto Parts (Shopify catalogues), plus Supercheap Auto, Repco, Autobarn and eBay Australia (new, Buy It Now listings). The last four are read from their normal search pages, so they are best effort: if a site changes its layout or blocks automated requests, PartHunter reports it as not responding and the other stores still return results.

Add any other store by its address under **Search sources**. PartHunter tries it as a Shopify store, then a WooCommerce store, then an ordinary search page. You can add up to 8.

## Ollama key (optional)

Under **Search sources**, paste an Ollama API key (free account; create one at https://ollama.com/settings/keys) or set `OLLAMA_API_KEY` before launching. PartHunter then uses Ollama's web search to find seller pages for part-number searches, falling back to DuckDuckGo and Bing if Ollama returns nothing. Prices are still read from each page, not from Ollama.

## Put it online as a website

PartHunter needs a small Python server (it reads store pages for you), so it can't be hosted as plain static files. Any host that runs a Dockerfile or Python will do. The included `Dockerfile` and `render.yaml` are set up for Render:

1. Put this folder in a GitHub repository.
2. On render.com choose **New → Blueprint**, pick the repository, and approve `render.yaml`.
3. In the service's **Environment** tab, add any keys you want the whole site to use: `OLLAMA_API_KEY`, `PARTHUNTER_SEARCHAPI_KEY`, `PARTHUNTER_SERPAPI_KEY`. Visitors can also paste their own under Search sources.
4. Open the `onrender.com` address. Over https, phones can use **Add to Home Screen**.

Website mode (`PARTHUNTER_PUBLIC=1`) lets anyone search, limits each visitor to 20 searches a minute, and still serves the API at `/api/v1` and its docs at `/docs`. Set `PARTHUNTER_API_KEYS` to require a key for the API. Any key you set on the server is spent by every visitor's searches, so keep the rate limit low.

Stores are more likely to block requests from a hosting provider than from your home connection, so expect more "not responding" results online than on your own computer. Free Render services also sleep when idle, so the first search after a quiet period is slow.
