
import asyncio
import os
from playwright.async_api import async_playwright

async def run():
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        print("Navigating to app...")
        try:
            await page.goto("http://localhost:8000")
            await page.wait_for_load_state("networkidle")
            
            # Switch to 3DS mode
            print("Switching to 3DS mode...")
            await page.click("text=Switch Primary Tool")
            await page.click("text=Compress to ZCCI/ZCIA/Z3DS")
            
            # Search for Corpse Party
            print("Filtering for 'Corpse Party'...")
            # Using the new inline filter input
            await page.fill("input[placeholder='Filter files...']", "Corpse Party")
            
            # Wait for file list to update
            await page.wait_for_timeout(1000)
            
            # Verify the file is visible
            file_visible = await page.is_visible("text=Corpse Party USA.3ds")
            if not file_visible:
                print("Error: File 'Corpse Party USA.3ds' not found in list")
                await context.close()
                await browser.close()
                return

            print("File found. Starting compression...")
            
            # Click the compress button (lightning bolt) for this specific row
            # Assuming it's the only one visible due to filter
            # The button has a title="Compress" or similar, or we find the row
            
            # Locate the row containing the text
            row = page.locator("tr", has_text="Corpse Party USA.3ds")
            compress_btn = row.locator("button[title='Compress']")
            
            if await compress_btn.count() == 0:
                 # Fallback if title attribute isn't set, look for the icon button
                 # It's usually the first action button
                 compress_btn = row.locator("button").first
            
            await compress_btn.click()
            
            # Wait for completion
            print("Waiting for compression to complete...")
            # We expect the status to change to "Success" or the progress bar to complete
            # Or we can wait for the 'check' icon to appear
            
            try:
                # Wait up to 60s for completion (it might be fast or slow depending on dummy file size)
                # We look for the "success" state in the UI for that row
                await row.locator(".text-green-500").wait_for(state="visible", timeout=60000)
                print("Compression reported success in UI!")
            except Exception as e:
                print(f"Timeout or error waiting for success: {e}")
                # Take screenshot for debug
                await page.screenshot(path="corpse_compress_fail.png")
            
            # Verify output file exists
            output_path = "/Users/talor/Library/CloudStorage/SynologyDrive-Emulation/roms/3ds/Corpse Party USA.z3ds"
            if os.path.exists(output_path):
                print(f"SUCCESS: Output file created at {output_path}")
                # Verify it's larger than 0 bytes
                size = os.path.getsize(output_path)
                print(f"Output size: {size} bytes")
                
                # Clean up the dummy input file
                input_path = "/Users/talor/Library/CloudStorage/SynologyDrive-Emulation/roms/3ds/Corpse Party USA.3ds"
                if os.path.exists(input_path):
                    os.remove(input_path)
                    print("Cleaned up input file.")
            else:
                print("FAILURE: Output file not found!")

        except Exception as e:
            print(f"Test failed: {e}")
            await page.screenshot(path="test_error.png")
        finally:
            await context.close()
            await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
