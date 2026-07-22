"""Stima dei token e budgeting del contesto.

Il sistema è multi-provider (OpenAI, Anthropic, Gemini): non esiste un
tokenizer unico e corretto per tutti, e trascinare `tiktoken` (specifico
OpenAI) darebbe una precisione illusoria sugli altri provider oltre ad
appesantire le dipendenze. Serve invece una stima *conservativa* e uniforme,
usata come budget di sicurezza per due scopi:

1. contenere il costo delle chiamate LLM (meno token di contesto = meno spesa);
2. non sforare la finestra di contesto del provider configurato dal cliente.

La stima ~4 caratteri/token è lo standard di fatto per testo in alfabeto
latino; per gli estratti conto italiani (numeri, date, nomi) tende a
sovrastimare leggermente i token, il che è esattamente la direzione prudente
per un budget.
"""

# Caratteri per token: costante di stima. Volutamente bassa (prudente) così il
# budget in token non viene mai sottostimato.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Stima conservativa del numero di token di una stringa."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def fit_segments_to_budget(
    segments: list[str],
    max_tokens: int,
    separator_tokens: int = 2,
) -> tuple[list[str], bool]:
    """Tiene i primi segmenti che rientrano nel budget in token.

    I segmenti arrivano già ordinati per rilevanza (rrf_score decrescente),
    quindi troncare la coda significa scartare i chunk meno rilevanti — la
    scelta giusta quando si deve tagliare per costo/finestra di contesto.

    Args:
        segments: I testi dei chunk, dal più al meno rilevante.
        max_tokens: Budget massimo in token per l'insieme.
        separator_tokens: Token attribuiti al separatore fra un chunk e l'altro.

    Returns:
        (kept, truncated) — `truncated` è True se almeno un segmento è stato
        scartato per rispettare il budget.
    """
    if max_tokens <= 0:
        return segments, False

    kept: list[str] = []
    used = 0
    truncated = False

    for segment in segments:
        cost = estimate_tokens(segment) + (separator_tokens if kept else 0)
        # Il primo segmento viene sempre tenuto anche se da solo supera il
        # budget: una risposta su un contesto tagliato è meglio di nessun
        # contesto, e il provider tronca comunque lato suo se necessario.
        if kept and used + cost > max_tokens:
            truncated = True
            continue
        kept.append(segment)
        used += cost

    return kept, truncated


def batch_by_char_budget(
    rows: list,
    max_chars: int,
    max_items: int,
    text_of=lambda row: row["text"],
) -> list[list]:
    """Raggruppa le righe in batch limitati sia per caratteri che per numero.

    Serve al percorso esaustivo (map-reduce): batch a solo numero fisso di
    chunk possono sforare il contesto quando i chunk sono lunghi (tabelle),
    mentre batch a soli caratteri possono creare batch con troppi frammenti.
    Un batch si chiude al primo dei due limiti raggiunto.
    """
    batches: list[list] = []
    current: list = []
    current_chars = 0

    for row in rows:
        row_chars = len(text_of(row))
        if current and (
            current_chars + row_chars > max_chars or len(current) >= max_items
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(row)
        current_chars += row_chars

    if current:
        batches.append(current)

    return batches
