export function assertLocal(request: Request) {
  const host = new URL(`http://${request.headers.get('host') || 'invalid'}`).hostname;
  if(!['127.0.0.1','localhost','[::1]'].includes(host)) throw new Error('This app is available on localhost only.');
  const origin=request.headers.get('origin');
  if(origin && (new URL(origin).host !== request.headers.get('host') || !['http:','https:'].includes(new URL(origin).protocol))) throw new Error('Cross-origin requests are not allowed.');
  if(request.headers.get('sec-fetch-site')==='cross-site') throw new Error('Cross-site requests are not allowed.');
}
export function failure(error: unknown, status=400) { return Response.json({error:error instanceof Error?error.message:'Request failed.'},{status}); }
