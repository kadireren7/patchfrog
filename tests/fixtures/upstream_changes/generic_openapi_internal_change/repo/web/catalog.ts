export async function getProduct(id: string) {
  const res = await fetch(`/v1/products/${id}`);
  return res.json();
}

export async function listProducts(query: string) {
  const res = await fetch("/v1/products" + query);
  return res.json();
}
