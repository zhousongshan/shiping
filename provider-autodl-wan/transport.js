// Diagnostics deliberately exclude URLs, request bodies and credentials.
export function transportFacts(error) {
  const cause=error?.cause ?? error;
  return {name:String(error?.name ?? 'Error'),code:String(cause?.code ?? ''),
    message:String(cause?.message ?? error?.message ?? 'transport failed')
      .replace(/https?:\/\/\S+/g,'[url]').slice(0,300)};
}

export async function checkConnection(base, fetcher=fetch) {
  // A read-only check catches known broken DNS/TLS/proxy routing before POST.
  // It does not prove a later POST was unaccepted if that POST loses its receipt.
  try {
    await fetcher(new URL(base).origin,{method:'HEAD',signal:AbortSignal.timeout(15000)});
  } catch(error) {
    const facts=transportFacts(error);
    throw Error('VIDEO_TRANSPORT_PREFLIGHT_FAILED: '+facts.code+' '+facts.message);
  }
}
